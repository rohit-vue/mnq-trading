"""
=============================================================================
CONNECTION MANAGER (v2.2)
=============================================================================
Manages TWS/IB Gateway connection with auto-reconnect capability.

Features:
- Automatic reconnection when TWS/Gateway disconnects or restarts
- IB Gateway daily restart handling (auto-reconnect after restart)
- Exponential backoff for reconnection attempts
- Connection health monitoring (periodic heartbeat)
- Event callbacks for connection status changes
- Error 1100/1102 handling (connectivity loss/restore without TCP drop)
- Extended disconnect alerting via callback
- Log deduplication for repeated connection errors

IMPORTANT: IB Gateway restarts daily (IBKR regulatory requirement).
This manager handles it gracefully by:
1. Detecting the disconnect
2. Waiting for Gateway to come back (auto-login must be enabled)
3. Reconnecting and resyncing all data/positions
=============================================================================
"""

import asyncio
import logging
from typing import Optional, Callable, List
from dataclasses import dataclass
from datetime import datetime, time as dtime
import pytz

from ib_async import IB

logger = logging.getLogger(__name__)


@dataclass
class ConnectionConfig:
    """Connection configuration."""
    host: str = "127.0.0.1"
    port: int = 7497  # Paper trading default
    client_id: int = 1
    readonly: bool = False
    timeout: float = 4.0
    max_reconnect_attempts: int = 0  # 0 = infinite
    initial_delay: float = 5.0
    max_delay: float = 60.0
    backoff_multiplier: float = 2.0


class ConnectionManager:
    """
    Manages IB connection with automatic reconnection.
    
    This class wraps the ib_async IB client and provides:
    - Auto-reconnect on disconnect
    - Connection health monitoring (heartbeat every 30s)
    - Graceful handling of IB Gateway daily restarts
    - Infinite retry support for 24/7 operation
    
    IB Gateway Daily Restart:
    ========================
    IB Gateway restarts once daily (regulatory requirement, cannot be disabled).
    Default restart time is ~11:45 PM ET (3:45 AM UAE / 8:45 AM PKT).
    
    To handle this:
    1. Enable "Auto Restart" in IB Gateway: Configure > Settings > Lock and Exit
    2. Set restart time to CME daily maintenance window (5:00-6:00 PM ET)
    3. This manager will auto-reconnect after Gateway comes back
    """
    
    def __init__(
        self,
        config: Optional[ConnectionConfig] = None,
        on_connect: Optional[Callable] = None,
        on_disconnect: Optional[Callable] = None,
        on_reconnect: Optional[Callable] = None,
        on_extended_disconnect: Optional[Callable] = None
    ):
        """
        Initialize connection manager.

        Parameters:
        -----------
        config : ConnectionConfig
            Connection configuration
        on_connect : Callable
            Callback when connected
        on_disconnect : Callable
            Callback when disconnected
        on_reconnect : Callable
            Callback when reconnected (after auto-reconnect)
        on_extended_disconnect : Callable
            Callback when reconnection has been failing for extended period
            (called after 30 failed attempts). Signature: callback(attempt_count)
        """
        self.config = config or ConnectionConfig()
        self.ib = IB()

        # Callbacks
        self._on_connect_callback = on_connect
        self._on_disconnect_callback = on_disconnect
        self._on_reconnect_callback = on_reconnect
        self._on_extended_disconnect_callback = on_extended_disconnect

        # State
        self._is_running = False
        self._reconnect_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._reconnect_count = 0
        self._current_delay = self.config.initial_delay
        self._shutdown_requested = False
        self._last_connected_time: Optional[datetime] = None
        self._total_reconnects = 0

        # Error 1100/1102 state tracking
        self._connectivity_lost = False  # True while in 1100 state
        self._last_1100_time: Optional[datetime] = None
        self._1100_count = 0  # Count of 1100 errors since last recovery

        # Register disconnect handler
        self.ib.disconnectedEvent += self._on_disconnect
        self.ib.errorEvent += self._on_error

        logger.info("Connection Manager v2.2 initialized")
    
    async def connect(self) -> bool:
        """
        Establish initial connection to TWS/Gateway.
        
        Returns:
        --------
        bool
            True if connected successfully
        """
        try:
            logger.info(f"Connecting to {self.config.host}:{self.config.port}...")
            
            await self.ib.connectAsync(
                host=self.config.host,
                port=self.config.port,
                clientId=self.config.client_id,
                readonly=self.config.readonly,
                timeout=self.config.timeout
            )
            
            self._is_running = True
            self._reconnect_count = 0
            self._current_delay = self.config.initial_delay
            self._last_connected_time = datetime.now()
            
            logger.info(f"Connected to Interactive Brokers")
            
            if self._on_connect_callback:
                try:
                    result = self._on_connect_callback()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.error(f"Error in connect callback: {e}")
            
            # Start health monitoring
            self._start_health_monitor()
            
            return True
            
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            return False
    
    def _on_disconnect(self) -> None:
        """Handle disconnect event."""
        if self._shutdown_requested:
            logger.info("Disconnect due to shutdown request")
            return
        
        logger.warning("Disconnected from TWS/Gateway")
        
        # Stop health monitor
        self._stop_health_monitor()
        
        if self._on_disconnect_callback:
            try:
                self._on_disconnect_callback()
            except Exception as e:
                logger.error(f"Error in disconnect callback: {e}")
        
        # Start reconnection if running
        if self._is_running and not self._reconnect_task:
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())
    
    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract) -> None:
        """
        Handle error events that may indicate connection issues.

        Key error codes:
        - 1100: Connectivity lost (TCP stays alive, no disconnectedEvent)
        - 1101: Connectivity restored, data lost (need full resync)
        - 1102: Connectivity restored, data maintained (still need feed restart)
        - 2110: TWS-server connectivity broken
        - 10182: Failed to request live updates (feed subscription dead)
        """
        if errorCode in [1100, 2110]:
            # Connectivity lost — log only on first occurrence to avoid spam
            if not self._connectivity_lost:
                self._connectivity_lost = True
                self._last_1100_time = datetime.now()
                self._1100_count = 0
                logger.warning(f"Connection error {errorCode}: {errorString}")
            else:
                self._1100_count += 1
                # Log a summary every 20 occurrences instead of every time
                if self._1100_count % 20 == 0:
                    elapsed = (datetime.now() - self._last_1100_time).total_seconds()
                    logger.warning(
                        f"Connection error {errorCode} persisting for {elapsed:.0f}s "
                        f"({self._1100_count} occurrences)"
                    )

        elif errorCode in [1101, 1102]:
            elapsed = 0
            if self._last_1100_time:
                elapsed = (datetime.now() - self._last_1100_time).total_seconds()

            logger.info(
                f"Connection restored {errorCode}: {errorString} "
                f"(was down for {elapsed:.0f}s, {self._1100_count} error events)"
            )

            # Reset connectivity loss state
            self._connectivity_lost = False
            self._last_1100_time = None
            self._1100_count = 0
            self._current_delay = self.config.initial_delay

            # CRITICAL: Trigger reconnect callback to restart feed and resync
            # Error 1100->1102 does NOT fire disconnectedEvent (TCP stays alive)
            # so on_reconnect is never called via _reconnect_loop.
            # We must call it here to restart the dead realtime feed.
            if self._on_reconnect_callback and self._is_running:
                logger.info("Triggering reconnect handler after 1100->1102 recovery...")
                try:
                    result = self._on_reconnect_callback()
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)
                except Exception as e:
                    logger.error(f"Error in reconnect callback after 1102: {e}")

        elif errorCode == 10182:
            # Feed subscription died — this confirms the realtime feed is dead
            logger.warning(f"Live data subscription lost (error {errorCode}): {errorString}")
    
    async def _reconnect_loop(self) -> None:
        """
        Reconnection loop with exponential backoff.

        Handles both normal disconnects and IB Gateway daily restarts.
        During daily restart, Gateway may be down for 1-3 minutes.
        The bot keeps retrying until Gateway comes back.

        With max_reconnect_attempts=0, this loops FOREVER (24/7 mode).

        Extended disconnect alerting:
        - After 30 failed attempts (~30 min), calls on_extended_disconnect callback
        - After 100 failed attempts, reduces log frequency to every 50 attempts
        """
        _extended_alert_sent = False

        while self._is_running and not self._shutdown_requested:
            self._reconnect_count += 1

            # Check max attempts (0 = infinite)
            if self.config.max_reconnect_attempts > 0:
                if self._reconnect_count > self.config.max_reconnect_attempts:
                    logger.error(f"Max reconnection attempts ({self.config.max_reconnect_attempts}) reached")
                    self._is_running = False
                    break

            # Adaptive logging to avoid flooding the log
            if self._reconnect_count <= 3:
                logger.info(f"Reconnection attempt {self._reconnect_count} in {self._current_delay:.1f}s...")
            elif self._reconnect_count <= 30 and self._reconnect_count % 10 == 0:
                logger.info(f"Still reconnecting... attempt {self._reconnect_count} "
                           f"(Gateway may be restarting)")
            elif self._reconnect_count % 50 == 0:
                logger.info(f"Still reconnecting... attempt {self._reconnect_count} "
                           f"(Gateway has been down for ~{self._reconnect_count} min)")

            # Alert user via callback after extended disconnect (30 attempts ~ 30 min)
            if self._reconnect_count == 30 and not _extended_alert_sent:
                _extended_alert_sent = True
                logger.warning(
                    f"Extended disconnect: {self._reconnect_count} failed reconnection attempts. "
                    f"Gateway may require manual intervention."
                )
                if self._on_extended_disconnect_callback:
                    try:
                        result = self._on_extended_disconnect_callback(self._reconnect_count)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        logger.error(f"Error in extended disconnect callback: {e}")

            await asyncio.sleep(self._current_delay)

            # Don't try to reconnect if shutdown requested
            if self._shutdown_requested:
                break

            try:
                # Check if already connected
                if self.ib.isConnected():
                    logger.info("Already connected")
                    break

                await self.ib.connectAsync(
                    host=self.config.host,
                    port=self.config.port,
                    clientId=self.config.client_id,
                    readonly=self.config.readonly,
                    timeout=self.config.timeout
                )

                self._total_reconnects += 1
                self._last_connected_time = datetime.now()

                logger.info(
                    f"Reconnected successfully after {self._reconnect_count} attempts "
                    f"(total reconnects: {self._total_reconnects})"
                )

                # Reset counters
                self._reconnect_count = 0
                self._current_delay = self.config.initial_delay

                # Restart health monitor
                self._start_health_monitor()

                # Call reconnect callback (resyncs positions, data feeds, etc.)
                if self._on_reconnect_callback:
                    try:
                        result = self._on_reconnect_callback()
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        logger.error(f"Error in reconnect callback: {e}")

                break

            except Exception as e:
                # Only log first 3 failures in detail, then reduce noise
                if self._reconnect_count <= 3:
                    logger.warning(f"Reconnection failed: {e}")
                elif self._reconnect_count <= 30 and self._reconnect_count % 10 == 0:
                    logger.warning(f"Reconnection attempt {self._reconnect_count} failed: {e}")
                elif self._reconnect_count % 50 == 0:
                    logger.warning(f"Reconnection attempt {self._reconnect_count} failed: {e}")

                # Increase delay with exponential backoff
                self._current_delay = min(
                    self._current_delay * self.config.backoff_multiplier,
                    self.config.max_delay
                )

        self._reconnect_task = None
    
    # =========================================================================
    # HEALTH MONITORING
    # =========================================================================
    
    def _start_health_monitor(self) -> None:
        """Start periodic connection health checks."""
        self._stop_health_monitor()
        self._health_task = asyncio.create_task(self._health_check_loop())
    
    def _stop_health_monitor(self) -> None:
        """Stop health check task."""
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
        self._health_task = None
    
    async def _health_check_loop(self) -> None:
        """
        Periodic health check to detect silent disconnections.
        
        Some disconnections don't trigger the disconnectedEvent.
        This catches them by checking if the connection is alive.
        """
        try:
            while self._is_running and not self._shutdown_requested:
                await asyncio.sleep(30)  # Check every 30 seconds
                
                if not self.ib.isConnected():
                    logger.warning("Health check: Connection lost (silent disconnect)")
                    # Trigger reconnection
                    if not self._reconnect_task:
                        if self._on_disconnect_callback:
                            try:
                                self._on_disconnect_callback()
                            except Exception as e:
                                logger.error(f"Error in disconnect callback: {e}")
                        self._reconnect_task = asyncio.create_task(self._reconnect_loop())
                    break
                    
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Health check error: {e}")
    
    # =========================================================================
    # DISCONNECT & CLEANUP
    # =========================================================================
    
    async def disconnect(self) -> None:
        """Gracefully disconnect and stop reconnection attempts."""
        logger.info("Disconnecting...")
        
        self._shutdown_requested = True
        self._is_running = False
        
        # Stop health monitor
        self._stop_health_monitor()
        
        # Cancel reconnection task
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None
        
        # Disconnect IB
        if self.ib.isConnected():
            self.ib.disconnect()
        
        logger.info("Disconnected")
    
    def is_connected(self) -> bool:
        """Check if currently connected."""
        return self.ib.isConnected()
    
    @property
    def client(self) -> IB:
        """Get the underlying IB client."""
        return self.ib
    
    @property
    def reconnect_stats(self) -> dict:
        """Get reconnection statistics."""
        return {
            'total_reconnects': self._total_reconnects,
            'last_connected': self._last_connected_time,
            'is_connected': self.is_connected(),
            'is_reconnecting': self._reconnect_task is not None,
            'current_attempt': self._reconnect_count
        }
