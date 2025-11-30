### THIS IS A WORK IN PROGRESS... COMING IN EARLY 2026

My attempt to make a usable Meshtastic-Bitchat Bridge that works in both directions


# BitChat-Meshtastic Bridge (`mesh-bit.py`)

A Python bridge that relays messages from **BitChat (Bluetooth LE)** to the **Meshtastic (LoRa Mesh)** network as well as in the opposite direction via a Linux host. 

---

## Requirements

### Hardware
* **Linux Host:** PC, Laptop, or Raspberry Pi with a working **Bluetooth LE** adapter.
* **Meshtastic Device:** LoRa radio (e.g., Heltec V3, T-Beam) connected to the host via **USB**.
* **BitChat Device:** A smartphone running the BitChat app.

### Software
* **Python 3.x**
* **Dependencies:**
  ```bash
  pip install meshtastic bleak pubsub-py
