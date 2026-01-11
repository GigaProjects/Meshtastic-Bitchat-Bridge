import asyncio
import logging
import sys
import time
import struct
import random
import signal
import hashlib
import hashlib
from typing import Optional, List, Dict

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
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(BRIDGE_TAG)

class MeshtasticHandler:
    def __init__(self, port: str, loop: asyncio.AbstractEventLoop):
        self.port = port
        self.interface = None
        self.loop = loop
        self.ble_handler: Optional['BitchatBLEHandler'] = None

    def set_ble_handler(self, handler):
        self.ble_handler = handler

    def start(self):
        try:
            logger.info(f"Attempting connection to Meshtastic on {self.port}...")
            self.interface = meshtastic.serial_interface.SerialInterface(self.port)
            pub.subscribe(self.on_receive, "meshtastic.receive")
            logger.info("Meshtastic interface ready.")
        except Exception as e:
            logger.error(f"Failed to connect to Meshtastic: {e}")
            logger.info("Trying alternative port /dev/ttyUSB0...")
            try:
                self.interface = meshtastic.serial_interface.SerialInterface("/dev/ttyUSB0")
                pub.subscribe(self.on_receive, "meshtastic.receive")
                logger.info("Meshtastic interface ready on /dev/ttyUSB0.")
            except Exception as e2:
                logger.error(f"Failed secondary connection: {e2}")
                logger.info("Continuing in Bluetooth-only mode.")

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
        if self.interface:
            try:
                logger.info(f"[Meshtastic] Sending packet: {text}")
                self.interface.sendText(text)
            except Exception as e:
                logger.error(f"[Meshtastic] Send failed: {e}")

class BitchatBLEHandler:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.connected_clients: Dict[str, BleakClient] = {}
        self.connecting_devices = set()
        self.meshtastic_handler: Optional[MeshtasticHandler] = None
        self.loop = loop
        self._stopping = False
        
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
        
        logger.info(f"Bridge Identity: {self.my_id.hex()}")
        logger.info(f"Full public key: {self.public_key_bytes.hex()}")  # Add this for debugging

    def set_meshtastic_handler(self, handler):
        self.meshtastic_handler = handler

    async def run_scanner(self):
        """Continuously scan for Bitchat devices (Polling Mode)"""
        logger.info("Scanning for Bitchat devices...")
        
        while not self._stopping:
            try:
                # Scan for 3 seconds
                devices = await BleakScanner.discover(timeout=3.0, return_adv=True)
                
                for device, adv in devices.values():
                    if BITCHAT_SERVICE_UUID.lower() in adv.service_uuids:
                        # Only connect if not already connected or connecting
                        if (device.address not in self.connected_clients and 
                            device.address not in self.connecting_devices):
                            logger.info(f"Found peer: {device.address}")
                            self.connecting_devices.add(device.address)
                            asyncio.create_task(self.connect_client(device))
                            
            except Exception as e:
                logger.warning(f"Scanner error: {e}")
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
        logger.info(f"Timestamp: {timestamp_ms} (Hex: {struct.pack('>Q', timestamp_ms).hex()})")
        
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
        
        logger.info(f"Unsigned block (PADDED) hex for signing: {unsigned_packet_padded.hex()}")
        
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
            client = BleakClient(device)
            try:
                logger.info(f"Connection attempt {attempt + 1}/{MAX_RETRIES} to {device.address}")
                await client.connect()
                if client.is_connected:
                    logger.info(f"✅ Connected to {device.address}")
                    self.connected_clients[device.address] = client
                    
                    # Send Handshake (ANNOUNCE)
                    logger.info("Sending Handshake...")
                    
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
                    
                    # Setup notification handler
                    await client.start_notify(BITCHAT_TX_CHAR_UUID, self._create_notification_handler(device.address))
                    
                    return  # Success!
                    
            except Exception as e:
                error_msg = str(e)
                logger.warning(f"Connection error: {error_msg}")
                
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
                        logger.info(f"⏳ Transient error, retrying in {delay:.1f}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        # Fatal error, don't retry
                        logger.error(f"❌ Fatal connection error, giving up")
                        break
                else:
                    logger.error(f"❌ Failed to connect after {MAX_RETRIES} attempts")
                    break
        
        # Clean up if all retries failed
        self.connecting_devices.discard(device.address)
    
    def _on_client_disconnect(self, address: str):
        """Callback when a BLE client disconnects"""
        logger.info(f"Client {address} disconnected")
        if address in self.connected_clients:
            del self.connected_clients[address]
        self.connecting_devices.discard(address)

    def _create_notification_handler(self, address):
        def handler(sender_handle: int, data: bytearray):
            try:
                if len(data) < HEADER_SIZE: return
                
                # Parse Header
                packet_type = data[1]
                flags = data[11]
                payload_len = struct.unpack('>H', data[12:14])[0]
                
                has_recipient = (flags & FLAG_HAS_RECIPIENT) != 0
                is_compressed = (flags & FLAG_IS_COMPRESSED) != 0
                
                offset = HEADER_SIZE
                
                # Sender ID
                sender_id = data[offset : offset+8]
                offset += 8
                short_id = sender_id.hex()[-4:]
                

                if has_recipient:
                    recipient_id = data[offset : offset+8]  # Log the recipient before skipping
                    logger.info(f"Recipient ID: {recipient_id.hex()}")
                    if all(b == 0xff for b in recipient_id):
                        logger.info("Incoming is broadcast message")
                    else:
                        logger.info("Incoming is private message")
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
                    logger.info(f"(BLE -> Bridge) {short_id}: {final_text}")
                    
                    if self.meshtastic_handler:
                        self.meshtastic_handler.send_text(f"[Bit:{short_id}] {final_text}")
                    



                   


            except Exception as e:
                logger.error(f"Packet processing error: {e}")
        return handler


    async def broadcast(self, message: str, recipient_id: Optional[bytes] = None):
        if self._stopping: return
        try:
            payload = message.encode('utf-8')
            packet = self._build_packet(PACKET_TYPE_MESSAGE, payload, recipient_id)
            
            logger.info(f"Echo packet hex (padded): {packet.hex()}")  # Add this for debugging

            current_clients = list(self.connected_clients.values())
            for client in current_clients:
                if client.is_connected:
                    await client.write_gatt_char(BITCHAT_RX_CHAR_UUID, packet, response=True)
                    logger.info(f"Echo sent to {client.address}")
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
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def handle_sigint():
        stop_event.set()

    loop.add_signal_handler(signal.SIGINT, handle_sigint)
    
    ble_handler = BitchatBLEHandler(loop)
    meshtastic_handler = MeshtasticHandler(MESHTASTIC_PORT, loop)
    
    ble_handler.set_meshtastic_handler(meshtastic_handler)
    meshtastic_handler.set_ble_handler(ble_handler)
    
    meshtastic_handler.start()
    scanner_task = asyncio.create_task(ble_handler.run_scanner())
    
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
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
