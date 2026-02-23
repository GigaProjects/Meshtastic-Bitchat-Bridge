import asyncio
import logging
import sys
import time
import struct
import random
import signal
import hashlib
from typing import Optional, List, Dict
from collections import deque

# Cryptography
import nacl.signing
import nacl.encoding
import nacl.bindings

# Compression
import lz4.block

# BLE & LoRa
from bleak import BleakScanner, BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
import meshtastic
import meshtastic.serial_interface
from pubsub import pub

# --- CONFIGURATION ---
MESHTASTIC_PORT = "/dev/ttyACM0" 

# Bitchat UUIDs
BITCHAT_SERVICE_UUID = "f47b5e2d-4a9e-4c5a-9b3f-8e1d2c3a4b5c"
BITCHAT_RX_CHAR_UUID = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d" 
BITCHAT_TX_CHAR_UUID = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"

BRIDGE_TAG = "Bridge"

# Protocol Constants (Matches BinaryProtocol.kt)
PACKET_VERSION = 0x01
PACKET_TYPE_ANNOUNCE = 0x01
PACKET_TYPE_MESSAGE = 0x02
PACKET_TTL = 0x07
FLAG_HAS_RECIPIENT = 0x01
FLAG_HAS_SIGNATURE = 0x02
FLAG_IS_COMPRESSED = 0x04
CANONICAL_TTL_FOR_SIGNING = 0x00  # Fixed per Kotlin comment for relay compatibility

# V1 Header: Ver(1)+Type(1)+TTL(1)+Time(8)+Flags(1)+Len(2) = 14 bytes?
# Kotlin says HEADER_SIZE_V1 = 13. Let's re-count.
# Ver(1) + Type(1) + TTL(1) + Time(8) + Flags(1) + Len(2) = 14.
# Wait, Kotlin code: 
# buffer.put(version), buffer.put(type), buffer.put(ttl) -> 3 bytes
# buffer.putLong(timestamp) -> 8 bytes (Total 11)
# buffer.put(flags) -> 1 byte (Total 12)
# buffer.putShort(len) -> 2 bytes (Total 14)
# There is a discrepancy in the Kotlin comment vs code.
# Kotlin constant says HEADER_SIZE_V1 = 13.
# BUT the code writes 14 bytes. 
# Let's stick to the calculated 14 bytes as per the actual `buffer.put` calls.
HEADER_SIZE = 14 

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(BRIDGE_TAG)

class MeshtasticHandler:
    def __init__(self, port: str, loop: asyncio.AbstractEventLoop):
        self.port = port
        self.interface = None
        self.loop = loop
        self.ble_handler: Optional['BitchatBLEHandler'] = None
        self._reconnect_delay = 5  # Initial reconnect delay in seconds
        self._max_reconnect_delay = 20  # Max backoff delay (reduced for faster recovery)
        self._stopping = False
        self._subscribed = False  # Track if we already subscribed to pubsub topics

    def set_ble_handler(self, handler):
        self.ble_handler = handler

    def _try_connect(self) -> bool:
        """Attempt to connect to Meshtastic on primary or fallback port. Returns True on success."""
        ports_to_try = [self.port, "/dev/ttyUSB0"]
        for port in ports_to_try:
            try:
                logger.debug(f"Attempting connection to Meshtastic on {port}...")
                self.interface = meshtastic.serial_interface.SerialInterface(port)
                
                # Get local node info for a more professional log
                my_node = self.interface.getMyNodeInfo()
                node_user = my_node.get('user', {})
                long_name = node_user.get('longName', 'Unknown')
                node_id = node_user.get('id', 'Unknown')
                
                # Subscribe to pubsub topics only once (they persist across reconnects)
                if not self._subscribed:
                    pub.subscribe(self.on_receive, "meshtastic.receive")
                    pub.subscribe(self.on_connection_lost, "meshtastic.connection.lost")
                    self._subscribed = True
                
                logger.info(f"Meshtastic interface ready on {port} (Node: {long_name} / {node_id})")
                self._reconnect_delay = 5  # Reset backoff on success
                return True
            except Exception as e:
                logger.debug(f"Failed to connect on {port}: {e}")
        
        return False

    def start(self):
        """Initial connection attempt."""
        if not self._try_connect():
            logger.warning("Meshtastic not available. Will keep retrying in the background.")
            asyncio.run_coroutine_threadsafe(self._reconnect_loop(), self.loop)

    def on_connection_lost(self, interface=None):
        """Called when the Meshtastic serial connection is lost."""
        logger.warning("Meshtastic connection lost! Cleaning up and scheduling reconnect...")
        self._cleanup_interface()
        # Schedule the async reconnect loop from any thread
        asyncio.run_coroutine_threadsafe(self._reconnect_loop(), self.loop)

    def _cleanup_interface(self):
        """Safely close the old interface."""
        if self.interface:
            try:
                self.interface.close()
            except Exception:
                pass
            self.interface = None

    async def _reconnect_loop(self):
        """Attempt to reconnect with exponential backoff."""
        delay = self._reconnect_delay
        while not self._stopping and self.interface is None:
            logger.debug(f"Attempting Meshtastic reconnect in {delay}s...")
            await asyncio.sleep(delay)
            
            if self._stopping:
                break
            
            if self._try_connect():
                logger.info("Meshtastic reconnected successfully!")
                return
            
            # Exponential backoff
            delay = min(delay * 2, self._max_reconnect_delay)
        
        if self._stopping:
            logger.info("Reconnect loop stopped (shutdown requested).")

    def get_sender_name(self, from_id: str) -> str:
        if self.interface and self.interface.nodes:
            node = self.interface.nodes.get(from_id)
            if node:
                user = node.get('user')
                if user:
                    return user.get('longName', from_id)
        return from_id

    def on_receive(self, packet, interface):
        try:
            if 'decoded' in packet and 'text' in packet['decoded']:
                text = packet['decoded']['text']
                sender_id = packet['fromId']
                
                if text.startswith("[Bit:"): 
                    return

                sender_name = self.get_sender_name(sender_id)
                logger.info(f"(LoRa -> Bridge) {sender_name}: {text}")

                formatted_msg = f"[Mesh: {sender_name}] {text}"

                if self.ble_handler:
                    asyncio.run_coroutine_threadsafe(
                        self.ble_handler.broadcast(formatted_msg),
                        self.loop
                    )
        except Exception as e:
            logger.error(f"Error processing LoRa packet: {e}")

    def send_text(self, text: str):
        if self.interface is None:
            logger.debug(f"Cannot relay to LoRa — interface is disconnected: {text}")
            return
        try:
            logger.info(f"(Bridge -> LoRa) {text}")
            self.interface.sendText(text)
        except Exception as e:
            logger.error(f"LoRa relay failed: {e}")
            # If send fails, the connection is likely dead — trigger reconnect
            logger.warning("Send failure suggests connection is dead. Triggering reconnect...")
            self.on_connection_lost()

    def stop(self):
        """Stop reconnect attempts and close the interface."""
        self._stopping = True
        self._cleanup_interface()

class BitchatBLEHandler:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.connected_clients: Dict[str, BleakClient] = {}
        self.connecting_devices = set()
        self.meshtastic_handler: Optional[MeshtasticHandler] = None
        self.loop = loop
        self._stopping = False
        self.peer_nicknames = {}  # Store mapping of sender_id (hex) -> nickname
        self.failed_devices = {}  # Track devices that failed connection (address -> timestamp)
        self.processed_packets = deque(maxlen=100) # (sender_id, timestamp) for de-duplication
        self._nickname_tasks = {} # (sender_id -> asyncio.Task) for debouncing Name changes for de-duplication
        
        # --- IDENTITY SETUP ---
        # Use random key for fresh identity on every run to avoid stale peer state on phone
        self.signing_key = nacl.signing.SigningKey.generate() 
        self.verify_key = self.signing_key.verify_key
        self.public_key_bytes = self.verify_key.encode(encoder=nacl.encoding.RawEncoder)
        # self.my_id is now derived from X25519 key below
        
        self.x25519_private = nacl.bindings.crypto_box_keypair()[1]
        self.x25519_public = nacl.bindings.crypto_scalarmult_base(self.x25519_private)
        
        # Use SHA256 hash of Ed25519 (Signing) Public Key for SenderID
        # This matches the app's behavior where Identity is tied to the Signing Key.
        self.my_id = hashlib.sha256(self.public_key_bytes).digest()[:8]
        
        logger.debug(f"Bridge Identity: {self.my_id.hex()}")
        logger.debug(f"Full public key: {self.public_key_bytes.hex()}")

    def set_meshtastic_handler(self, handler):
        self.meshtastic_handler = handler

    async def _log_nickname_deferred(self, short_id: str, sender_hex: str):
        """Wait briefly before logging a nickname change to debounce typing events ('t' -> 'te' -> 'tes' -> 'test')"""
        await asyncio.sleep(1.0) # Wait 1 second of stable typing
        final_nickname = self.peer_nicknames.get(sender_hex)
        if final_nickname:
            logger.info(f"Peer {short_id} is now known as '{final_nickname}'")
            
    async def run_scanner(self):
        """Continuously scan for Bitchat devices (Polling Mode)"""
        logger.info("Scanning for Bitchat devices...")
        
        while not self._stopping:
            try:
                # Scan for 3 seconds
                devices = await BleakScanner.discover(timeout=3.0, return_adv=True)
                
                for device, adv in devices.values():
                    if BITCHAT_SERVICE_UUID.lower() in adv.service_uuids:
                        # Check blacklist (cool-down for 5 minutes)
                        if device.address in self.failed_devices:
                            if time.time() - self.failed_devices[device.address] < 300:
                                continue
                            else:
                                del self.failed_devices[device.address] # Expired

                        # Only connect if not already connected or connecting
                        if (device.address not in self.connected_clients and 
                            device.address not in self.connecting_devices):
                            logger.debug(f"Found peer: {device.address}")
                            self.connecting_devices.add(device.address)
                            asyncio.create_task(self.connect_client(device))
                            
            except Exception as e:
                logger.debug(f"Scanner error: {e}")
                await asyncio.sleep(1.0)
            
            await asyncio.sleep(0.5)

    def _pad_data(self, data: bytearray) -> bytearray:
        """PKCS7-style padding to optimal block size (matches MessagePadding.kt)"""
        # Block sizes from MessagePadding.kt
        block_sizes = [256, 512, 1024, 2048]
        
        # Account for encryption overhead (~16 bytes) - though we aren't encrypting here, 
        # the app expects padding to align with these blocks assuming encryption overhead.
        # But wait, BinaryProtocol.kt calls pad(result, optimalSize) on the PLAINTEXT packet bytes.
        # So we should pad the packet bytes to these sizes.
        
        target_size = len(data)
        for size in block_sizes:
            if len(data) + 16 <= size: # +16 is from optimalBlockSize logic in Kotlin
                target_size = size
                break
        
        if len(data) >= target_size:
            return data
            
        padding_needed = target_size - len(data)
        
        # Constrain to 255 (byte limit)
        if padding_needed > 255:
            # If we can't pad to the target block size with < 255 bytes, 
            # we just don't pad (or pad to a smaller alignment if we were strictly following PKCS7 block alignment, 
            # but here we are padding to specific privacy block sizes).
            # The Kotlin code says: "if (paddingNeeded > 255) return data"
            return data
            
        # PKCS#7: All pad bytes are equal to the pad length
        padding = bytes([padding_needed] * padding_needed)
        padded = bytearray(data)
        padded.extend(padding)
        return padded

    def _build_packet(self, type_byte, payload, recipient_id=None):
        """Builds a signed packet compliant with BinaryProtocol.kt
        
        CRITICAL: The signature must be computed over the PADDED unsigned packet,
        because toBinaryDataForSigning() on Android calls BinaryProtocol.encode()
        which applies padding BEFORE returning the bytes to verify.
        """
        
        # 1. Build Header (14 Bytes)
        header = bytearray()
        header.append(PACKET_VERSION)
        header.append(type_byte)
        header.append(PACKET_TTL)
        
        timestamp_ms = int(time.time() * 1000)
        header.extend(struct.pack('>Q', timestamp_ms))
        logger.debug(f"Timestamp: {timestamp_ms} (Hex: {struct.pack('>Q', timestamp_ms).hex()})")
        
        flags = FLAG_HAS_SIGNATURE
        if recipient_id:
            flags |= FLAG_HAS_RECIPIENT
        header.append(flags)
        
        header.extend(struct.pack('>H', len(payload)))
        
        # 2. Build UNSIGNED packet (for signing)
        # This must match what toBinaryDataForSigning() produces:
        # - TTL = SYNC_TTL_HOPS (0)
        # - Flags without HAS_SIGNATURE
        # - Then PADDED
        signing_header = bytearray(header)
        signing_header[2] = CANONICAL_TTL_FOR_SIGNING  # TTL = 0
        signing_flags = flags & ~FLAG_HAS_SIGNATURE  # Remove HAS_SIGNATURE
        signing_header[11] = signing_flags
        
        unsigned_packet = bytearray()
        unsigned_packet.extend(signing_header)
        unsigned_packet.extend(self.my_id)
        if recipient_id:
            unsigned_packet.extend(recipient_id[:8])
        unsigned_packet.extend(payload)
        
        # CRITICAL: Pad the unsigned packet BEFORE signing
        # This matches BinaryProtocol.encode() behavior
        unsigned_packet_padded = self._pad_data(unsigned_packet)
        
        logger.debug(f"Unsigned block (PADDED) hex for signing: {unsigned_packet_padded.hex()}")
        
        # 3. Sign the PADDED unsigned packet
        signature = self.signing_key.sign(bytes(unsigned_packet_padded)).signature
        
        # 4. Assemble Final Packet (with actual TTL and HAS_SIGNATURE flag)
        final = bytearray()
        final.extend(header)
        final.extend(self.my_id)
        if recipient_id:
            final.extend(recipient_id[:8])
        final.extend(payload)
        final.extend(signature)
        
        # 5. Apply Padding to final packet
        final_padded = self._pad_data(final)
        
        return bytes(final_padded)

    async def connect_client(self, device: BLEDevice):
        """Connect to a BLE device with retry logic for transient failures"""
        MAX_RETRIES = 3
        RETRY_DELAY = 2.0  # seconds
        
        for attempt in range(MAX_RETRIES):
            # Use address string instead of device object to force fresh resolution
            # This helps with iOS RPA rotation and avoids stale BlueZ cache
            client = BleakClient(device.address, timeout=10.0, disconnected_callback=self._on_client_disconnect)
            try:
                logger.debug(f"Connection attempt {attempt + 1}/{MAX_RETRIES} to {device.address}")
                await client.connect()
                if client.is_connected:
                    logger.info(f"Connected to {device.address}")
                    self.connected_clients[device.address] = client
                    
                    # Setup notification handler BEFORE handshake to avoid losing response
                    await client.start_notify(BITCHAT_TX_CHAR_UUID, self._create_notification_handler(device.address))
                    
                    # Send Handshake (ANNOUNCE)
                    logger.debug("Sending Handshake...")
                    
                    # ANNOUNCE Payload - Restore Tagged Structure
                    handshake_payload = bytearray()
                    name = "MeshBridge"
                    
                    # Tag 1: Name
                    handshake_payload.extend(b'\x01')
                    handshake_payload.append(len(name))
                    handshake_payload.extend(name.encode('utf-8'))
                    
                    # Tag 2: Noise Public Key (X25519) - 0x02
                    handshake_payload.extend(b'\x02')
                    handshake_payload.append(len(self.x25519_public))
                    handshake_payload.extend(self.x25519_public)
                    
                    # Tag 3: Signing Public Key (Ed25519) - 0x03
                    handshake_payload.extend(b'\x03')
                    handshake_payload.append(len(self.public_key_bytes))
                    handshake_payload.extend(self.public_key_bytes)
                    
                    # Send ANNOUNCE with EXPLICIT BROADCAST recipient
                    # This ensures the packet has the HAS_RECIPIENT flag set, which might be required for processing.
                    packet = self._build_packet(PACKET_TYPE_ANNOUNCE, handshake_payload, recipient_id=b'\xff'*8)
                    await client.write_gatt_char(BITCHAT_RX_CHAR_UUID, packet, response=True)
                    self.connecting_devices.discard(device.address)
                    
                    return  # Success!
                    
            except Exception as e:
                error_msg = str(e)
                logger.debug(f"Connection error: {error_msg}")
                
                # Clean up failed connection
                try:
                    if client.is_connected:
                        await client.disconnect()
                except:
                    pass
                
                # Retry logic
                if attempt < MAX_RETRIES - 1:
                    if "InProgress" in error_msg or "br-connection-canceled" in error_msg:
                        # These are transient errors, retry with backoff
                        delay = RETRY_DELAY * (2 ** attempt)  # Exponential backoff
                        logger.debug(f"Transient error, retrying in {delay:.1f}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        # Fatal error, don't retry
                        logger.debug(f"Fatal connection error for {device.address}, giving up")
                        break
                else:
                    logger.debug(f"Failed to connect to {device.address} after {MAX_RETRIES} attempts")
                    # Add to blacklist to avoid spamming pairing requests
                    self.failed_devices[device.address] = time.time()
                    break
        
        # Clean up if connection failed or loop ended
        self.connecting_devices.discard(device.address)
    
    def _on_client_disconnect(self, client: BleakClient):
        """Callback when a BLE client disconnects"""
        address = client.address
        logger.debug(f"BLE client {address} disconnected")
        if address in self.connected_clients:
            del self.connected_clients[address]
        self.connecting_devices.discard(address)

    def _create_notification_handler(self, address):
        def handler(sender_handle: int, data: bytearray):
            try:
                if len(data) < HEADER_SIZE: return
                
                # Parse Header
                packet_type = data[1]
                timestamp_raw = data[3:11]
                flags = data[11]
                payload_len = struct.unpack('>H', data[12:14])[0]
                
                has_recipient = (flags & FLAG_HAS_RECIPIENT) != 0
                is_compressed = (flags & FLAG_IS_COMPRESSED) != 0
                
                offset = HEADER_SIZE
                
                # Sender ID
                sender_id = data[offset : offset+8]
                offset += 8
                short_id = sender_id.hex()[-4:]
                
                # De-duplication Check
                packet_id = (sender_id, timestamp_raw)
                if packet_id in self.processed_packets:
                    return # Skip duplicate
                self.processed_packets.append(packet_id)

                if has_recipient:
                    recipient_id = data[offset : offset+8]  # Log the recipient before skipping
                    logger.debug(f"Recipient ID: {recipient_id.hex()}")
                    if all(b == 0xff for b in recipient_id):
                        logger.debug("Incoming is broadcast message")
                    else:
                        logger.debug("Incoming is private message")
                    offset += 8
                    
                # Extract Payload
                raw_payload = data[offset : offset + payload_len]
                
                # Decompression
                final_text = ""
                if is_compressed:
                    try:
                        # Skip original size (2 bytes) for lz4 block decompression
                        compressed_data = bytes(raw_payload[2:]) 
                        uncompressed_data = lz4.block.decompress(compressed_data, uncompressed_size=65536)
                        final_text = uncompressed_data.decode('utf-8', errors='ignore')
                    except Exception as e:
                        logger.error(f"Decompression failed: {e}")
                        return 
                else:
                    final_text = raw_payload.decode('utf-8', errors='ignore')

                if packet_type == PACKET_TYPE_MESSAGE:
                    sender_hex = sender_id.hex()
                    display_name = self.peer_nicknames.get(sender_hex, short_id)
                    
                    logger.info(f"(BLE -> Bridge) {display_name}: {final_text}")
                    
                    if self.meshtastic_handler:
                        self.meshtastic_handler.send_text(f"[Bit:{display_name}] {final_text}")
                
                elif packet_type == PACKET_TYPE_ANNOUNCE:
                    try:
                        # Parse TLV (Type-Length-Value)
                        tlv_offset = 0
                        while tlv_offset < len(raw_payload):
                            if tlv_offset + 2 > len(raw_payload): break
                            tag = raw_payload[tlv_offset]
                            length = raw_payload[tlv_offset + 1]
                            tlv_offset += 2
                            if tlv_offset + length > len(raw_payload): break
                            value = raw_payload[tlv_offset:tlv_offset + length]
                            tlv_offset += length
                            
                            if tag == 0x01: # Nickname Tag
                                nickname = value.decode('utf-8', errors='ignore')
                                sender_hex = sender_id.hex()
                                
                                # Bitchat sends progressive name updates char-by-char during initial typing.
                                # Let's debounce these using an asyncio task that waits 1.0 second.
                                old_nickname = self.peer_nicknames.get(sender_hex)
                                
                                if old_nickname != nickname:
                                    self.peer_nicknames[sender_hex] = nickname
                                    
                                    # Cancel any existing pending log task for this user
                                    if sender_hex in self._nickname_tasks:
                                        self._nickname_tasks[sender_hex].cancel()
                                        
                                    # Create a new debounced logging task
                                    self._nickname_tasks[sender_hex] = asyncio.create_task(
                                        self._log_nickname_deferred(short_id, sender_hex)
                                    )
                                    
                                break
                    except Exception as e:
                        logger.warning(f"Failed to parse ANNOUNCE: {e}")

            except Exception as e:
                logger.error(f"Packet processing error: {e}")
        return handler


    async def broadcast(self, message: str, recipient_id: Optional[bytes] = None):
        if self._stopping: return
        try:
            payload = message.encode('utf-8')
            packet = self._build_packet(PACKET_TYPE_MESSAGE, payload, recipient_id)
            
            logger.debug(f"Echo packet hex (padded): {packet.hex()}")

            current_clients = list(self.connected_clients.values())
            for client in current_clients:
                if client.is_connected:
                    await client.write_gatt_char(BITCHAT_RX_CHAR_UUID, packet, response=True)
                    logger.debug(f"Broadcast sent to {client.address}")
        except Exception as e:
            logger.error(f"Broadcast failed: {e}")



    async def stop(self):
        self._stopping = True
        logger.info("Disconnecting clients...")
        for client in list(self.connected_clients.values()):
            try:
                await client.disconnect()
            except Exception:
                pass
        self.connected_clients.clear()

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Meshtastic-Bitchat Bridge")
    parser.add_argument("--port", default=MESHTASTIC_PORT, help="Meshtastic serial port")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def handle_sigint():
        stop_event.set()

    loop.add_signal_handler(signal.SIGINT, handle_sigint)
    
    ble_handler = BitchatBLEHandler(loop)
    meshtastic_handler = MeshtasticHandler(args.port, loop)
    
    ble_handler.set_meshtastic_handler(meshtastic_handler)
    meshtastic_handler.set_ble_handler(ble_handler)
    
    meshtastic_handler.start()
    scanner_task = asyncio.create_task(ble_handler.run_scanner())
    
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
        meshtastic_handler.stop()
        await ble_handler.stop()
        scanner_task.cancel()
        try:
            await scanner_task
        except asyncio.CancelledError:
            pass
        logger.info("Shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
