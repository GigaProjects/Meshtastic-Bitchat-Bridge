# Mesh-Bit Bridge


https://github.com/user-attachments/assets/1a6bbe64-2a4b-4703-8669-91dd1d708aa4


A bidirectional bridge connecting Bitchat and Meshtastic. This tool allows local Bluetooth chat clients to communicate over long distances via LoRa radios, effectively extending the range of Bitchat using the Meshtastic network.

## How It Works

The script runs on a Linux device (like a Raspberry Pi or laptop) that has both Bluetooth and a USB connection to a Meshtastic device.

1.  **Bitchat to Meshtastic:** The bridge scans for nearby Bitchat devices over Bluetooth. When it receives a message from a phone, it relays that message to the Meshtastic LoRa network.
2.  **Meshtastic to Bitchat:** The bridge listens for incoming LoRa messages from the Meshtastic network. When a message arrives, it broadcasts it over Bluetooth to all connected Bitchat clients.

## Prerequisites

*   A Linux computer with a Bluetooth adapter.
*   A Meshtastic device connected via USB (e.g., /dev/ttyUSB0 or /dev/ttyACM0).
*   Python 3.7 or higher.

## Installation

1.  Clone this repository.
2.  Create a virtual environment (recommended):
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```

3.  Install the required Python libraries:
    ```bash
    pip install -r requirements.txt
    ```

## Usage

1.  Connect your Meshtastic device to the computer via USB.
2.  Ensure your Bluetooth adapter is active.
3.  Run the bridge script:
    ```bash
    python3 mesh-bit.py
    ```

4.  Open the Bitchat app on your phone and bring it close to the bridge. It should connect automatically.

## Troubleshooting

Bluetooth on Linux can sometimes become unresponsive or get stuck in a bad state. If the bridge fails to discover devices or connect, follow these steps to reset the system.

1.  Stop the script by pressing Ctrl+C.

2.  Force kill any lingering Python processes that might be holding the adapter:
    ```bash
    pkill -f "python.*mesh-bit"
    ```

3.  Restart the Bluetooth service (requires sudo):
    ```bash
    sudo systemctl restart bluetooth
    ```

4.  (Optional) If issues persist, you can try resetting the HCI interface directly:
    ```bash
    sudo hciconfig hci0 down
    sudo hciconfig hci0 up
    ```

5.  Run the script again.

## Credits

This project was built with help and inspiration from the following open source projects:

*   **bitchat-python**: https://github.com/kaganisildak/bitchat-python
*   **Meshtastic Python**: https://github.com/meshtastic/Meshtastic-python
