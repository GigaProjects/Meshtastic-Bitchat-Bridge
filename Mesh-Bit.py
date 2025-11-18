import asyncio
import logging
import sys
from typing import Optional, List, Dict

# Third-party imports
from bleak import BleakScanner, BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
import meshtastic
import meshtastic.serial_interface
from pubsub import pub

# --- CONFIGURATION ---
MESHTASTIC_PORT = "/dev/ttyUSB0"  # Update this for your OS (e.g., COM3)

# Bitchat Public Service UUIDs (Placeholders - Replace with actual Bitchat UUIDs)
# For "Public" chats, apps often use a specific Service UUID to broadcast presence.
BITCHAT_SERVICE_UUID = "00000000-0000-0000-0000-000000000000"
BITCHAT_RX_CHAR_UUID = "00000000-0000-0000-0000-000000000001" 
BITCHAT_TX_CHAR_UUID = "00000000-0000-0000-0000-000000000002"

# The name used to identify this bridge on the networks
BRIDGE_TAG = "Bridge"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(BRIDGE_TAG)

class MeshtasticHandler:
    """
    Handles the Long Range (LoRa) side of the bridge.
    """
    def __init__(self, port: str, loop: asyncio.AbstractEventLoop):
        self.port = port
        self.interface = None
        self.loop = loop
        self.ble_handler: Optional['BitchatBLEHandler'] = None

    def set_ble_handler(self, handler):
        self.ble_handler = handler

    def start(self):
        """Connects to the LoRa radio."""
        try:
            logger.info(f"Connecting to Meshtastic on {self.port}...")
            self.interface = meshtastic.serial_interface.SerialInterface(self.port)
            
            # Subscribe to the 'meshtastic.receive' topic to catch all incoming packets
            pub.subscribe(self.on_receive, "meshtastic.receive")
            logger.info("Meshtastic interface ready.")
        except Exception as e:
            logger.error(f"Failed to connect to Meshtastic: {e}")
            sys.exit(1)

    def get_sender_name(self, from_id: str) -> str:
        """
        Looks up the friendly name (LongName) of a node using its ID.
        """
        if self.interface and self.interface.nodes:
            node = self.interface.nodes.get(from_id)
            if node:
                user = node.get('user')
                if user:
                    return user.get('longName', from_id)
        return from_id

    def on_receive(self, packet, interface):
        """
        Triggered when a LoRa packet is received.
        """
        try:
            if 'decoded' in packet and 'text' in packet['decoded']:
                text = packet['decoded']['text']
                sender_id = packet['fromId']
                
                # Ignore messages sent by the bridge itself to prevent echo loops
                # (Meshtastic usually filters these, but good to be safe)
                if text.startswith("[Bit:"): 
                    return

                # 1. Resolve Identity
                sender_name = self.get_sender_name(sender_id)
                logger.info(f"(LoRa -> Bridge) {sender_name}: {text}")

                # 2. Format for Bitchat
                # We wrap the name so Bitchat users know who actually sent it.
                formatted_msg = f"[Mesh: {sender_name}] {text}"

                # 3. Forward to BLE (Thread-safe)
                if self.ble_handler:
                    asyncio.run_coroutine_threadsafe(
                        self.ble_handler.broadcast(formatted_msg),
                        self.loop
                    )
        except Exception as e:
            logger.error(f"Error processing LoRa packet: {e}")

    def send_text(self, text: str):
        """Sends text to the LoRa mesh."""
        if self.interface:
            # Meshtastic handles the queueing internally
            self.interface.sendText(text)

class BitchatBLEHandler:
    """
    Handles the Bluetooth Low Energy side of the bridge.
    """
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.connected_clients: Dict[str, BleakClient] = {}
        self.meshtastic_handler: Optional[MeshtasticHandler] = None
        self.loop = loop

    def set_meshtastic_handler(self, handler):
        self.meshtastic_handler = handler

    async def run_scanner(self):
        """Scans for Bitchat devices and maintains connections."""
        logger.info("Scanning for Bitchat devices...")
        
        def detection_callback(device: BLEDevice, advertisement_data: AdvertisementData):
            # Only connect if we see the specific Bitchat Service
            if BITCHAT_SERVICE_UUID.lower() in advertisement_data.service_uuids:
                if device.address not in self.connected_clients:
                    logger.info(f"Found new Bitchat peer: {device.address}")
                    asyncio.create_task(self.connect_client(device))

        scanner = BleakScanner(detection_callback)
        await scanner.start()
        
        # Keep the scanner running forever to find new people walking by
        while True:
            await asyncio.sleep(5)

    async def connect_client(self, device: BLEDevice):
        """Connects to a Bitchat peer and listens for their messages."""
        client = BleakClient(device)
        try:
            await client.connect()
            if client.is_connected:
                logger.info(f"Connected to {device.address}")
                self.connected_clients[device.address] = client
                
                # Listen for incoming data (notifications)
                await client.start_notify(BITCHAT_TX_CHAR_UUID, self.create_notification_handler(device.address))
                
                # Keep connection alive
                while client.is_connected:
                    await asyncio.sleep(1)
                    
        except Exception as e:
            logger.warning(f"Connection lost with {device.address}: {e}")
        finally:
            self.connected_clients.pop(device.address, None)
            await client.disconnect()

    def create_notification_handler(self, address):
        """Creates a closure to capture the specific device address."""
        def handler(sender_handle: int, data: bytearray):
            try:
                # Decode the raw bytes to text
                text = data.decode('utf-8')
                
                # 1. Resolve Identity (Best Effort)
                # Since we don't have the full Bitchat User DB, we use a partial ID
                # or the BLE address to distinguish users.
                short_id = address.replace(":", "")[-4:] 
                
                logger.info(f"(BLE -> Bridge) {short_id}: {text}")

                # 2. Format for Meshtastic
                formatted_msg = f"[Bit:{short_id}] {text}"

                # 3. Forward to LoRa
                if self.meshtastic_handler:
                    self.meshtastic_handler.send_text(formatted_msg)

            except Exception as e:
                logger.error(f"Error decoding BLE message: {e}")
        return handler

    async def broadcast(self, message: str):
        """Sends a message to ALL connected Bitchat peers."""
        payload = message.encode('utf-8')
        
        # We copy the list to avoid errors if clients disconnect while we iterate
        current_clients = list(self.connected_clients.values())
        
        for client in current_clients:
            if client.is_connected:
                try:
                    await client.write_gatt_char(BITCHAT_RX_CHAR_UUID, payload, response=True)
                except Exception as e:
                    logger.error(f"Failed to send to {client.address}: {e}")

async def main():
    loop = asyncio.get_running_loop()
    
    # 1. Setup Handlers
    ble_handler = BitchatBLEHandler(loop)
    meshtastic_handler = MeshtasticHandler(MESHTASTIC_PORT, loop)
    
    # 2. Link Handlers (Cross-reference)
    ble_handler.set_meshtastic_handler(meshtastic_handler)
    meshtastic_handler.set_ble_handler(ble_handler)
    
    # 3. Start Systems
    meshtastic_handler.start() # Starts the Serial thread
    await ble_handler.run_scanner() # Starts the Async BLE loop

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)