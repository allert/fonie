#!/usr/bin/env python3
import serial
import sys
import time
import os

if len(sys.argv) < 2:
    print("Usage: python3 pico_uart_flash.py <firmware.bin>")
    sys.exit(1)

firmware_path = sys.argv[1]
if not os.path.exists(firmware_path):
    print(f"File not found: {firmware_path}")
    sys.exit(1)

with open(firmware_path, 'rb') as f:
    firmware_data = f.read()

file_size = len(firmware_data)
print(f"Firmware size: {file_size} bytes")

print("Stopping fonie service to free up /dev/ttyAMA0...")
os.system("sudo systemctl stop fonie")
time.sleep(1)

port = '/dev/ttyAMA5'
baud = 115200
chunk_size = 1024

try:
    ser = serial.Serial(port, baud, timeout=5)
    ser.reset_input_buffer()

    print("Sending ENTER_OTA...")
    ser.write(b'\n')
    time.sleep(0.1)
    ser.write(b'{"event":"ENTER_OTA"}\n')
    
    # Wait for OTA_READY
    ready = False
    for _ in range(10):
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        print(f"Pico: {line}")
        if 'OTA_READY' in line:
            ready = True
            break
    
    if not ready:
        print("Pico did not respond with OTA_READY. Aborting.")
        sys.exit(1)

    # Let the Pico finish its post-ready delay and buffer flush
    time.sleep(0.15)

    print(f"Sending file size: {file_size}")
    ser.write(f"{file_size}\n".encode())

    # Wait for OTA_BEGIN
    begin = False
    for _ in range(5):
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        print(f"Pico: {line}")
        if 'OTA_BEGIN' in line:
            begin = True
            break
            
    if not begin:
        print("Pico did not respond with OTA_BEGIN. Aborting.")
        sys.exit(1)

    print("Streaming firmware...")
    written = 0
    while written < file_size:
        end = min(written + chunk_size, file_size)
        chunk = firmware_data[written:end]
        
        # Write chunk
        ser.write(chunk)
        
        # Wait for ACK
        ack = False
        while True:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if line:
                # Ignore debug logs from Pico if any
                if line == 'ACK':
                    ack = True
                    break
                elif 'NACK_TIMEOUT' in line:
                    print("Pico reported chunk timeout!")
                    sys.exit(1)
                else:
                    print(f"Pico: {line}")
            else:
                break
                
        if not ack:
            print(f"Timeout waiting for ACK at offset {written}. Aborting.")
            sys.exit(1)
            
        written = end
        print(f"Progress: {written}/{file_size} bytes ({int((written/file_size)*100)}%)", end='\r')
        
    print("\nUpload complete! Waiting for OTA_SUCCESS...")
    success = False
    for _ in range(10):
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        print(f"Pico: {line}")
        if 'OTA_SUCCESS' in line:
            success = True
            break
            
    if success:
        print("OTA Successful! Pico is restarting...")
    else:
        print("Did not receive OTA_SUCCESS. Check Pico state.")

finally:
    ser.close()
    print("Starting fonie service...")
    os.system("sudo systemctl start fonie")
