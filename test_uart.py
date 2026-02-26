import serial
ser = serial.Serial('/dev/ttyAMA1', 115200, timeout=1)
print("Listening...")
while True:
    data = ser.readline()
    if data:
        print(data)
