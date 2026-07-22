import threading
import serial
import time
from datetime import datetime
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from colorama import Fore, Style, init
import os
import pandas as pd

sample_name = input("Enter the name for this evaporation run:")
 #_____________________SAVE THE DATA IN .TXT FILES____________________________________________
base_folder = 'evaporation data'
custom_folder_name = sample_name + ' evaporation data'  
final_folder_path = os.path.join(base_folder, custom_folder_name)
if not os.path.exists(base_folder):
    os.makedirs(base_folder)
if not os.path.exists(final_folder_path):
    os.makedirs(final_folder_path)

init()
stop_event = threading.Event()
data_lock = threading.Lock()


#_________________DEFINE GENERAL VARIABLES AND PARAMETERS______________________________________
device_info = {
    'CK-1 evaporator QMB': {'port': 'COM4', 'baud_rate': '115200'},
    'Sample QMB': {'port': 'COM16', 'baud_rate': '115200'},
    'XGS600 HFIG pressure': {'port': 'COM6', 'baud_rate': '9600'},
    'Oven PID temperature': {'port': 'COM9', 'baud_rate': '9600'},
    'Keysight power supply': {'port': 'COM17', 'baud_rate': '9600'},
    'Arduino CK-1 crucible temperature': {'port': 'COM3', 'baud_rate': '9600'},  
}
QMBs = {'CK-1 evaporator QMB',
        'Sample QMB',
}
timeout = 1
data = {
    'CK-1 evaporator QMB': {'thickness_times': [], 'rate_times': [], 'thickness_data': [], 'rate_data': []},
    'Sample QMB': {'thickness_times': [], 'rate_times': [], 'thickness_data': [], 'rate_data': []},
    'XGS600 HFIG pressure': {'pressure_times': [], 'pressure_data': []},
    'Oven PID temperature': {'temperature_times': [], 'temperature_data': []},
    'Keysight power supply': {'current_times': [], 'current_data': [], 'voltage_times': [], 'voltage_data': []},
    'Arduino CK-1 crucible temperature': {'temperature_times': [], 'temperature_data': []}, 
}


#_____________ALL THE QMBs BASE CODE_____________________________________________________________________
QMB__bytes = {
    "STX": b'\x02',
    "ADDR": b'\x10',
    "CMD_RSP": b'\x80',
    "CR": b'\x0D',
}
QMB__sub_commands = {
    'thickness': b'S',
    'rate': b'T',
    'zero': b'B',
}
def QMB__calculate_checksum(command):
    checksum = sum(command) % 256
    upper_nibble = (checksum >> 4) & 0x0F
    lower_nibble = checksum & 0x0F
    return bytes([upper_nibble + 0x30, lower_nibble + 0x30])
QMB__commands = {
    'thickness': QMB__bytes['STX'] + QMB__bytes['ADDR'] + QMB__bytes['CMD_RSP'] + QMB__sub_commands['thickness'] + QMB__calculate_checksum(QMB__bytes['ADDR'] + QMB__bytes['CMD_RSP'] + QMB__sub_commands['thickness']) + QMB__bytes['CR'],
    'rate': QMB__bytes['STX'] + QMB__bytes['ADDR'] + QMB__bytes['CMD_RSP'] + QMB__sub_commands['rate'] + QMB__calculate_checksum(QMB__bytes['ADDR'] + QMB__bytes['CMD_RSP'] + QMB__sub_commands['rate']) + QMB__bytes['CR'],
    'zero': QMB__bytes['STX'] + QMB__bytes['ADDR'] + QMB__bytes['CMD_RSP'] + QMB__sub_commands['zero'] + QMB__calculate_checksum(QMB__bytes['ADDR'] + QMB__bytes['CMD_RSP'] + QMB__sub_commands['zero']) + QMB__bytes['CR'],
}


#_________________INITIALIZE PLOTS FOR REALTIME PLOTTING______________________________________
fig, ((ax_thickness_ck1, ax_rate_ck1, ax_pressure_xgs600),
      (ax_thickness_sample, ax_rate_sample, ax_temperature_oven),
      (ax_current_keysight, ax_voltage_keysight, ax_temperature_ck1)) = plt.subplots(3, 3, figsize=(18, 15))

plt.subplots_adjust(left=0.05, right=0.99, top=0.9, bottom=0.1, hspace=0.4, wspace=0.25)

# Initialize lines with labels, colors, and add legends
line_thickness_ck1, = ax_thickness_ck1.plot([], [], label='CK-1 Thickness', color="green")
line_rate_ck1, = ax_rate_ck1.plot([], [], label='CK-1 Rate', color="green")
line_thickness_sample, = ax_thickness_sample.plot([], [], label="Sample Thickness", color="green")
line_rate_sample, = ax_rate_sample.plot([], [], label="Sample Rate", color="green")
line_pressure_xgs600, = ax_pressure_xgs600.plot([], [], label="HFIG Pressure", color="blue")
line_temperature_oven, = ax_temperature_oven.plot([], [], label="Oven Temperature", color="magenta")
line_current_keysight, = ax_current_keysight.plot([], [], label="Keysight Current", color="yellow")
line_voltage_keysight, = ax_voltage_keysight.plot([], [], label="Keysight Voltage", color="yellow")
line_temperature_ck1, = ax_temperature_ck1.plot([], [], label="CK-1 Crucible Temperature", color="red")

# Set plot titles
ax_thickness_ck1.set_title("CK-1 Evaporator QMB: Thickness (Å)")
ax_rate_ck1.set_title("CK-1 Evaporator QMB: Rate (Å/s)")
ax_thickness_sample.set_title("Sample QMB: Thickness (Å)")
ax_rate_sample.set_title("Sample QMB: Rate (Å/s)")
ax_pressure_xgs600.set_title("Synthesis chamber HFIG: Pressure (mbar)")
ax_temperature_oven.set_title("Oven PID: Temperature (ºC)")
ax_current_keysight.set_title("CK-1 crucible coil: Current (A)")
ax_voltage_keysight.set_title("CK-1 crucible coil: Voltage (V)")
ax_temperature_ck1.set_title("CK-1 crucible: Temperature (ºC)")

# Set custom axis labels
ax_thickness_ck1.set_xlabel("Time")
ax_thickness_ck1.set_ylabel(f"Thickness (Å)")
ax_rate_ck1.set_xlabel("Time")
ax_rate_ck1.set_ylabel("Rate (Å/s)")
ax_thickness_sample.set_xlabel("Time")
ax_thickness_sample.set_ylabel("Thickness (Å)")
ax_rate_sample.set_xlabel("Time")
ax_rate_sample.set_ylabel("Rate (Å/s)")
ax_pressure_xgs600.set_xlabel("Time")
ax_pressure_xgs600.set_ylabel("Pressure (mbar)")
ax_temperature_oven.set_xlabel("Time")
ax_temperature_oven.set_ylabel("Temperature (ºC)")
ax_current_keysight.set_xlabel("Time")
ax_current_keysight.set_ylabel("Current (A)")
ax_voltage_keysight.set_xlabel("Time")
ax_voltage_keysight.set_ylabel("Voltage (V)")
ax_temperature_ck1.set_xlabel("Time")
ax_temperature_ck1.set_ylabel("Temperature (ºC)")

# Set x axis timestamp format
ax_thickness_ck1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
ax_thickness_ck1.tick_params(axis='x', rotation=30)
ax_rate_ck1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
ax_rate_ck1.tick_params(axis='x', rotation=30)
ax_thickness_sample.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
ax_thickness_sample.tick_params(axis='x', rotation=30)
ax_rate_sample.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
ax_rate_sample.tick_params(axis='x', rotation=30)
ax_pressure_xgs600.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
ax_pressure_xgs600.tick_params(axis='x', rotation=30)
ax_temperature_oven.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
ax_temperature_oven.tick_params(axis='x', rotation=30)
ax_current_keysight.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
ax_current_keysight.tick_params(axis='x', rotation=30)
ax_voltage_keysight.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
ax_voltage_keysight.tick_params(axis='x', rotation=30)
ax_temperature_ck1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
ax_temperature_ck1.tick_params(axis='x', rotation=30)


#_________________FUNCTION TO UPDATE REAL TIME PLOTS______________________________________
def update_plot(): 
    while not stop_event.is_set():   
        with data_lock:
             #_____________________SAVE THE DATA IN .TXT FILES____________________________________________
            print('aaaaaa')
            
#             data = {
#     'CK-1 evaporator QMB': {'thickness_times': [], 'rate_times': [], 'thickness_data': [], 'rate_data': []},
#     'Sample QMB': {'thickness_times': [], 'rate_times': [], 'thickness_data': [], 'rate_data': []},
#     'XGS600 HFIG pressure': {'pressure_times': [], 'pressure_data': []},
#     'Oven PID temperature': {'temperature_times': [], 'temperature_data': []},
#     'Keysight power supply': {'current_times': [], 'current_data': [], 'voltage_times': [], 'voltage_data': []},
#     'Arduino CK-1 crucible temperature': {'temperature_times': [], 'temperature_data': []}, 
# }
#             df = pd.DataFrame()
#             for tool, params in data.items():
#                 for var_key, var_value in params.items():
#                     print('bbbb')
#                     df[var_key]=var_value
#             file_table_path = os.path.join(final_folder_path, "table.csv")
#             with open(file_table_path, 'w') as file:
#                 df.to_csv(file, '\t', header=False, index=False)        


#                       print('{} {}'.format(name, age))
#             dic = {'S00:D58': 1, 'M23:Q14': 1, 'S43:H52': 84, 'S43:H53': 2, 'S43:H50': 5, 'S43:H57': 1, 'M87:E11': 10}
    
#             originalDF = pd.DataFrame(dic.items()).rename(columns={0: 'key', 1: 'val'})
#             split_keysDF = pd.DataFrame(originalDF['key'].str.split(':').tolist())
#             finalDF = split_keysDF.join(originalDF['val'])

#             finalDF.to_csv('test.tsv', '\t', header=False, index=False)
            
#             originalDF= pd.DataFrame(data.items())
#             split_keysDF = pd.DataFrame(originalDF['key'].tolist())
#             finalDF = split_keysDF.join(originalDF['val'])
            
#             file_path = os.path.join(final_folder_path, f"{title.replace(' ', '_')}.txt")
#             file_path = os.path.join(final_folder_path, "table.csv")
#             with open(file_path, 'w') as file:
#                 pd.DataFrame(data.items()).to_csv(file, '\t', header=False, index=False)
# #                 finalDF.to_csv('test.tsv', '\t', header=False, index=False)
            
            for title, data_dict in data.items():
                file_path = os.path.join(final_folder_path, f"{title.replace(' ', '_')}.txt")
                with open(file_path, 'w') as file:
                    for key, values in data_dict.items():
                        file.write(f"{key}:\n")
                        file.write("\n".join(map(str, values)) + "\n\n")

            
            try:
                # Update CK-1 evaporator QMB plots
                if data['CK-1 evaporator QMB']['thickness_times'] and data['CK-1 evaporator QMB']['thickness_data']:
                    line_thickness_ck1.set_data(data['CK-1 evaporator QMB']['thickness_times'], data['CK-1 evaporator QMB']['thickness_data'])
                    ax_thickness_ck1.relim()
                    ax_thickness_ck1.autoscale_view()
                    latest_thickness_ck1 = data['CK-1 evaporator QMB']['thickness_data'][-1]
                    line_thickness_ck1.set_label(f"Latest CK-1 thickness: {latest_thickness_ck1:.2f} Å")                    
                    ax_thickness_ck1.legend()
                    
                if data['CK-1 evaporator QMB']['rate_times'] and data['CK-1 evaporator QMB']['rate_data']:
                    line_rate_ck1.set_data(data['CK-1 evaporator QMB']['rate_times'], data['CK-1 evaporator QMB']['rate_data'])
                    ax_rate_ck1.relim()
                    ax_rate_ck1.autoscale_view()
                    latest_rate_ck1 = data['CK-1 evaporator QMB']['rate_data'][-1]
                    line_rate_ck1.set_label(f"Latest CK-1 rate: {latest_rate_ck1:.2f} Å/s")                    
                    ax_rate_ck1.legend()

                # Update Sample QMB plots
                if data['Sample QMB']['thickness_times'] and data['Sample QMB']['thickness_data']:
                    line_thickness_sample.set_data(data['Sample QMB']['thickness_times'], data['Sample QMB']['thickness_data'])
                    ax_thickness_sample.relim()
                    ax_thickness_sample.autoscale_view()
                    latest_thickness_sample = data['Sample QMB']['thickness_data'][-1]
                    line_thickness_sample.set_label(f"Latest sample thickness: {latest_thickness_sample:.2f} Å")                    
                    ax_thickness_sample.legend()
                    
                if data['Sample QMB']['rate_times'] and data['Sample QMB']['rate_data']:
                    line_rate_sample.set_data(data['Sample QMB']['rate_times'], data['Sample QMB']['rate_data'])
                    ax_rate_sample.relim()
                    ax_rate_sample.autoscale_view()
                    latest_rate_sample = data['Sample QMB']['rate_data'][-1]
                    line_rate_sample.set_label(f"Latest sample rate: {latest_rate_sample:.2f} Å/s")                    
                    ax_rate_sample.legend()

                # Update XGS600 HFIG pressure plot
                if data['XGS600 HFIG pressure']['pressure_times'] and data['XGS600 HFIG pressure']['pressure_data']:
                    line_pressure_xgs600.set_data(data['XGS600 HFIG pressure']['pressure_times'], data['XGS600 HFIG pressure']['pressure_data'])
                    ax_pressure_xgs600.relim()
                    ax_pressure_xgs600.autoscale_view()
                    latest_pressure_xgs600 = data['XGS600 HFIG pressure']['pressure_data'][-1]
                    line_pressure_xgs600.set_label(f"Latest chamber pressure: {latest_pressure_xgs600:.2e} mbar")                    
                    ax_pressure_xgs600.legend()

                # Update Oven PID temperature plot
                if data['Oven PID temperature']['temperature_times'] and data['Oven PID temperature']['temperature_data']:
                    line_temperature_oven.set_data(data['Oven PID temperature']['temperature_times'], data['Oven PID temperature']['temperature_data'])
                    ax_temperature_oven.relim()
                    ax_temperature_oven.autoscale_view()
                    latest_temperature_oven = data['Oven PID temperature']['temperature_data'][-1]
                    line_temperature_oven.set_label(f"Latest oven temperature: {latest_temperature_oven:.0f} ºC")                    
                    ax_temperature_oven.legend()

                # Update Keysight power supply current plot
                if data['Keysight power supply']['current_times'] and data['Keysight power supply']['current_data']:
                    line_current_keysight.set_data(data['Keysight power supply']['current_times'], data['Keysight power supply']['current_data'])
                    ax_current_keysight.relim()
                    ax_current_keysight.autoscale_view()
                    latest_current_keysight = data['Keysight power supply']['current_data'][-1]
                    line_current_keysight.set_label(f"Latest coil current: {latest_current_keysight:.4f} A")                    
                    ax_current_keysight.legend()

                # Update Keysight power supply voltage plot
                if data['Keysight power supply']['voltage_times'] and data['Keysight power supply']['voltage_data']:
                    line_voltage_keysight.set_data(data['Keysight power supply']['voltage_times'], data['Keysight power supply']['voltage_data'])
                    ax_voltage_keysight.relim()
                    ax_voltage_keysight.autoscale_view()
                    latest_voltage_keysight = data['Keysight power supply']['voltage_data'][-1]
                    line_voltage_keysight.set_label(f"Latest coil voltage: {latest_voltage_keysight:.3f} V")                    
                    ax_voltage_keysight.legend()

                # Update Arduino CK-1 crucible temperature plot
                if data['Arduino CK-1 crucible temperature']['temperature_times'] and data['Arduino CK-1 crucible temperature']['temperature_data']:
                    line_temperature_ck1.set_data(data['Arduino CK-1 crucible temperature']['temperature_times'], data['Arduino CK-1 crucible temperature']['temperature_data'])
                    ax_temperature_ck1.relim()
                    ax_temperature_ck1.autoscale_view()
                    latest_temperature_ck1 = data['Arduino CK-1 crucible temperature']['temperature_data'][-1]
                    line_temperature_ck1.set_label(f"Latest crucible temperature: {latest_temperature_ck1:.2f} ºC")                    
                    ax_temperature_ck1.legend()

                plt.pause(0.1)  # Update every 0.1 seconds

            except Exception as e:
                    print(f"Error updating plot: {e}")

    
#_________________INITIALIZE CONNECTIONS______________________________________
# Initialize the last request times for thickness and rate for each device
last_request = {}
for key in device_info:
    last_request[key] = {
        'thickness': time.time(),
        'rate': time.time()
    }
# Open serial connections for each device in the ports dictionary
connections = {}
for key in device_info:
    connections[key] = serial.Serial(
        port=device_info[key]['port'],
        baudrate=device_info[key]['baud_rate'],
        timeout=timeout
    )


#_________________DEFINE THREAD FOR QMBs READING MOLECULE DEPOSITION______________________________________
def monitor_qmb():
    while not stop_event.is_set():     
        try:
            for key in QMBs:
                # Send zeroing command to reset thickness and timer
                connections[key].write(QMB__commands['zero'])
                time.sleep(0.1)
                print(f"{key}: Zeroing command sent.")

            while not stop_event.is_set(): 
                current_time = time.time()

                for key in QMBs:
                    # Request thickness every 1 second
                    if current_time - last_request[key]['thickness'] >= 1.0: 
                        connections[key].write(QMB__commands['thickness'])
                        time.sleep(0.1)
                        response_thickness = connections[key].read(connections[key].in_waiting or 64)

                        # Process thickness data
                        if response_thickness:
                            cropped_data = response_thickness[3:-3]
                            try:
                                thickness_value = float(cropped_data)
                                timestamp = datetime.now()
                                formatted_timestamp = timestamp.strftime('%Y-%m-%d %H:%M:%S')
                                decimals = f"{timestamp.microsecond // 10000:02d}"
                                data[key]['thickness_times'].append(timestamp)
                                data[key]['thickness_data'].append(thickness_value)
                                print(f"{formatted_timestamp}.{Fore.LIGHTBLACK_EX}{decimals}{Style.RESET_ALL} - {Fore.GREEN}{key} Thickness: {thickness_value} Å{Style.RESET_ALL}")
                            except ValueError:
                                print(f"{key}: Failed to parse thickness.")
                        last_request[key]['thickness'] = current_time

                    # Request rate every 0.5 seconds
                    if current_time - last_request[key]['rate'] >= 0.5:
                        connections[key].write(QMB__commands['rate'])
                        time.sleep(0.1)
                        response_rate = connections[key].read(connections[key].in_waiting or 64)

                        # Process rate data
                        if response_rate:
                            cropped_data = response_rate[3:-3]
                            try:
                                rate_value = float(cropped_data)
                                timestamp = datetime.now()
                                formatted_timestamp = timestamp.strftime('%Y-%m-%d %H:%M:%S')
                                decimals = f"{timestamp.microsecond // 10000:02d}"
                                data[key]['rate_times'].append(timestamp)
                                data[key]['rate_data'].append(rate_value)
                                print(f"{formatted_timestamp}.{Fore.LIGHTBLACK_EX}{decimals}{Style.RESET_ALL} - {Fore.GREEN}{key} Rate: {rate_value} Å/s{Style.RESET_ALL}")
                            except ValueError:
                                print(f"{key}: Failed to parse rate.")
                        last_request[key]['rate'] = current_time

        except serial.SerialException as e:
            print(f"Serial error in thread for {key}: {e}")
        except Exception as e:
            print(f"Error in thread for {key}: {e}")
    time.sleep(0.1)
    for key in QMBs:
        connections[key].close()
    print(">> Stop_event received. ▄︻̷̿┻̿═━一 QMBs reading stopping.")

    
#_________________DEFINE THREAD FOR XGS600 READING PRESSURE______________________________________    
def read_pressure():
    key = 'XGS600 HFIG pressure'
    while not stop_event.is_set():
        try:
            connections[key]
            time.sleep(1)  
            command = "#0002USYNTH\r" 
            connections[key].write(command.encode()) 
            time.sleep(0.1) 
            XGS600_message = connections[key].read(connections[key].in_waiting or 100)
            XGS600_message = XGS600_message.decode(errors='ignore').strip()
            XGS600_message = XGS600_message.lstrip('>')
            timestamp = datetime.now()
            formatted_timestamp = timestamp.strftime('%Y-%m-%d %H:%M:%S')
            decimals = f"{timestamp.microsecond // 10000:02d}"
            pressure_value = float(XGS600_message) 
            print(f"{formatted_timestamp}.{Fore.LIGHTBLACK_EX}{decimals}{Style.RESET_ALL} - {Fore.BLUE}Synthesis chamber pressure: {pressure_value:.2e} mbar{Style.RESET_ALL}")  
            data[key]['pressure_times'].append(timestamp)
            data[key]['pressure_data'].append(pressure_value)
            
        except serial.SerialException as e:
            print(f"Serial error in thread for {key}: {e}")
        except Exception as e:
            print(f"Error in thread for {key}: {e}") 
    
    time.sleep(0.1)
    connections[key].close()
    print("> Stop_event received. ▄︻̷̿┻̿═━一 XGS600 reading stopping.")    


#_________________DEFINE THREAD FOR KEYSIGHT POWERSUPPLY READING CURRENT AND VOLTAGE____________________  
def read_powersupply():
    key = 'Keysight power supply'
    while not stop_event.is_set():
        try:
            time.sleep(1)  
            connections[key].write(b'system:remote\n') #Force the powersupply into remote mode, blocking manual operation, to allow measurement
            time.sleep(0.1)
            connections[key].write(b'measure:voltage?\n') 
            time.sleep(0.1)  
            measured_voltage = connections[key].readline().decode().strip()
            connections[key].write(b'measure:current?\n') 
            time.sleep(0.1)  
            measured_current = connections[key].readline().decode().strip()
            connections[key].write(b'system:local\n') #Go back to manual operation to allow us to change source parameters manually
            time.sleep(0.1)
            
            timestamp = datetime.now()
            formatted_timestamp = timestamp.strftime('%Y-%m-%d %H:%M:%S')
            decimals = f"{timestamp.microsecond // 10000:02d}"
            current_value = float(measured_current)
            voltage_value = float(measured_voltage) 
            print(f"{formatted_timestamp}.{Fore.LIGHTBLACK_EX}{decimals}{Style.RESET_ALL} - {Fore.YELLOW}CK-1 crucible coil current: {current_value:.4f} A{Style.RESET_ALL}")
            print(f"{formatted_timestamp}.{Fore.LIGHTBLACK_EX}{decimals}{Style.RESET_ALL} - {Fore.YELLOW}CK-1 crucible coil voltage: {voltage_value:.4f} V{Style.RESET_ALL}")
            data[key]['current_times'].append(timestamp)
            data[key]['current_data'].append(current_value)
            data[key]['voltage_times'].append(timestamp)
            data[key]['voltage_data'].append(voltage_value)
            
        except serial.SerialException as e:
            print(f"Serial error in thread for {key}: {e}")
        except Exception as e:
            print(f"Error in thread for {key}: {e}") 
    
    time.sleep(0.1)
    connections[key].close()
    print("> Stop_event received. ▄︻̷̿┻̿═━一 Keysight powersupply reading stopping.") 
    

#_________________DEFINE THREAD FOR ARDUINO READING CK-1 CRUCIBLE THERMOCOUPLE______________________________________    
def read_PID():
    key = 'Oven PID temperature'
    while not stop_event.is_set():   
        try:
            connections[key]
            time.sleep(1)  
            command_EOT = chr(4)
            connections[key].write(command_EOT.encode()) 
            time.sleep(0.5) 
            identifier_PV = "M1"
            command_read_PV = "00" + identifier_PV + chr(5)  # ENQ
            connections[key].write(command_read_PV.encode()) 
            time.sleep(0.1) 
            PID_message = connections[key].read(connections[key].in_waiting or 100)
            PID_message_str = PID_message.decode(errors='ignore')
            temperature_str = PID_message_str[6:9]
             
            timestamp = datetime.now()
            formatted_timestamp = timestamp.strftime('%Y-%m-%d %H:%M:%S')
            decimals = f"{timestamp.microsecond // 10000:02d}"
            temperature_value = float(temperature_str)
            print(f"{formatted_timestamp}.{Fore.LIGHTBLACK_EX}{decimals}{Style.RESET_ALL} - {Fore.MAGENTA}Oven PID temperature: {temperature_value:.0f} ºC{Style.RESET_ALL}")  
            data[key]['temperature_times'].append(timestamp)
            data[key]['temperature_data'].append(temperature_value)

        except serial.SerialException as e:
            print(f"Serial error in thread for {key}: {e}")
        except Exception as e:
            print(f"Error in thread for {key}: {e}")

    time.sleep(0.1)
    connections[key].close()
    print(">> Stop_event received. ▄︻̷̿┻̿═━一 Oven PID reading stopping.")
        
        
#_________________DEFINE THREAD FOR ARDUINO READING CK-1 CRUCIBLE THERMOCOUPLE______________________________________    
def read_arduino():
    key = 'Arduino CK-1 crucible temperature'
    while not stop_event.is_set():   
        try:
            connections[key]
            time.sleep(1)  
            while not stop_event.is_set():
                if connections[key].in_waiting > 0:  # Check if data is available
                    arduino_message = connections[key].readline().decode('utf-8').strip()  # Read the data, decode it to string
                    timestamp = datetime.now()
                    formatted_timestamp = timestamp.strftime('%Y-%m-%d %H:%M:%S')
                    decimals = f"{timestamp.microsecond // 10000:02d}"
                    ck1_temperature_value = float(arduino_message)
                    data[key]['temperature_times'].append(timestamp)
                    data[key]['temperature_data'].append(ck1_temperature_value)
                    print(f"{formatted_timestamp}.{Fore.LIGHTBLACK_EX}{decimals}{Style.RESET_ALL} - {Fore.RED}CK-1 crucible temperature: {ck1_temperature_value} ºC{Style.RESET_ALL}")
                    
        except serial.SerialException as e:
            print(f"Serial error in thread for {key}: {e}")
        except Exception as e:
            print(f"Error in thread for {key}: {e}")

    time.sleep(0.1)
    connections[key].close()
    print(">> Stop_event received. ▄︻̷̿┻̿═━一 Arduino reading stopping.")
        
        
#_________________START MAIN FUNCTION______________________________________
def main():
    #_____________________MANAGE THE THREADS____________________________________________
    QMBs_thread = threading.Thread(target=monitor_qmb)
    XGS600_thread = threading.Thread(target=read_pressure)
    oven_PID_thread = threading.Thread(target=read_PID)
    evaporator_thermocouple_thread = threading.Thread(target=read_arduino)
    powersupply_thread = threading.Thread(target=read_powersupply)

    QMBs_thread.start()
    XGS600_thread.start()
    oven_PID_thread.start()
    evaporator_thermocouple_thread.start()
    powersupply_thread.start()

    try:
        while True:
            time.sleep(0.5)  # Main thread sleeps while pressure_thread is running
            update_plot()
            
    except KeyboardInterrupt:
        stop_event.set()
        print("(▀̿Ĺ̯▀̿ ̿) Stop all the threads!!!")
        
    QMBs_thread.join()
    XGS600_thread.join()
    oven_PID_thread.join()
    evaporator_thermocouple_thread.join() 
    powersupply_thread.join()

    print("> All threads stopped.\n ̿̿ ̿̿ ̿̿ ̿'̿'\̵͇̿̿\з= ( ▀ ͜͞ʖ▀) =ε/̵͇̿̿/’̿’̿ ̿ ̿̿ ̿̿ ̿̿")
    

    #_____________________DISPLAY THE FINAL PLOTS WITH ALL THE DATA____________________________________________
    fig, ((ax_thickness_ck1, ax_rate_ck1, ax_pressure_xgs600),
          (ax_thickness_sample, ax_rate_sample, ax_temperature_oven),
          (ax_current_keysight, ax_voltage_keysight, ax_temperature_ck1)) = plt.subplots(3, 3, figsize=(18, 15))
    
    fig.suptitle("Evaporation parameters", fontsize=16, fontweight='bold')

    plt.subplots_adjust(left=0.05, right=0.99, top=0.9, bottom=0.1, hspace=0.45, wspace=0.25)

    # CK-1 Evaporator QMB - Thickness and Rate
    ax_thickness_ck1.plot(data['CK-1 evaporator QMB']['thickness_times'], data['CK-1 evaporator QMB']['thickness_data'], '-o', color="green", markersize=4)
    ax_thickness_ck1.set_title("CK-1 Evaporator QMB Thickness")
    ax_thickness_ck1.set_xlabel("Time")
    ax_thickness_ck1.set_ylabel("Thickness (Å)")
    ax_thickness_ck1.tick_params(axis='x', rotation=30)

    ax_rate_ck1.plot(data['CK-1 evaporator QMB']['rate_times'], data['CK-1 evaporator QMB']['rate_data'], '-o', color="green", markersize=4)
    ax_rate_ck1.set_title("CK-1 Evaporator QMB Rate")
    ax_rate_ck1.set_xlabel("Time")
    ax_rate_ck1.set_ylabel("Rate (Å/s)")
    ax_rate_ck1.tick_params(axis='x', rotation=30)

    # Sample QMB - Thickness and Rate
    ax_thickness_sample.plot(data['Sample QMB']['thickness_times'], data['Sample QMB']['thickness_data'], '-o', color="green", markersize=4)
    ax_thickness_sample.set_title("Sample QMB Thickness")
    ax_thickness_sample.set_xlabel("Time")
    ax_thickness_sample.set_ylabel("Thickness (Å)")
    ax_thickness_sample.tick_params(axis='x', rotation=30)

    ax_rate_sample.plot(data['Sample QMB']['rate_times'], data['Sample QMB']['rate_data'], '-o', color="green", markersize=4)
    ax_rate_sample.set_title("Sample QMB Rate")
    ax_rate_sample.set_xlabel("Time")
    ax_rate_sample.set_ylabel("Rate (Å/s)")
    ax_rate_sample.tick_params(axis='x', rotation=30)

    # XGS600 HFIG - Pressure
    ax_pressure_xgs600.plot(data['XGS600 HFIG pressure']['pressure_times'], data['XGS600 HFIG pressure']['pressure_data'], '-o', color="blue", markersize=4)
    ax_pressure_xgs600.set_title("XGS600 HFIG Pressure")
    ax_pressure_xgs600.set_xlabel("Time")
    ax_pressure_xgs600.set_ylabel("Pressure (mbar)")
    ax_pressure_xgs600.tick_params(axis='x', rotation=30)

    # Oven PID - Temperature
    ax_temperature_oven.plot(data['Oven PID temperature']['temperature_times'], data['Oven PID temperature']['temperature_data'], '-o', color="magenta", markersize=4)
    ax_temperature_oven.set_title("Oven PID Temperature")
    ax_temperature_oven.set_xlabel("Time")
    ax_temperature_oven.set_ylabel("Temperature (ºC)")
    ax_temperature_oven.tick_params(axis='x', rotation=30)

    # Keysight Power Supply - Current and Voltage
    ax_current_keysight.plot(data['Keysight power supply']['current_times'], data['Keysight power supply']['current_data'], '-o', color="yellow", markersize=4)
    ax_current_keysight.set_title("Keysight Power Supply Current")
    ax_current_keysight.set_xlabel("Time")
    ax_current_keysight.set_ylabel("Current (A)")
    ax_current_keysight.tick_params(axis='x', rotation=30)

    ax_voltage_keysight.plot(data['Keysight power supply']['voltage_times'], data['Keysight power supply']['voltage_data'], '-o', color="yellow", markersize=4)
    ax_voltage_keysight.set_title("Keysight Power Supply Voltage")
    ax_voltage_keysight.set_xlabel("Time")
    ax_voltage_keysight.set_ylabel("Voltage (V)")
    ax_voltage_keysight.tick_params(axis='x', rotation=30)

    # Arduino CK-1 Crucible - Temperature
    ax_temperature_ck1.plot(data['Arduino CK-1 crucible temperature']['temperature_times'], data['Arduino CK-1 crucible temperature']['temperature_data'], '-o', color="red", markersize=4)
    ax_temperature_ck1.set_title("Arduino CK-1 Crucible Temperature")
    ax_temperature_ck1.set_xlabel("Time")
    ax_temperature_ck1.set_ylabel("Temperature (ºC)")
    ax_temperature_ck1.tick_params(axis='x', rotation=30)

    
#     #_____________________SAVE THE DATA IN .TXT FILES____________________________________________
#     base_folder = 'evaporation data'
#     custom_folder_name = sample_name + ' evaporation data'  
#     final_folder_path = os.path.join(base_folder, custom_folder_name)
#     if not os.path.exists(base_folder):
#         os.makedirs(base_folder)
#     if not os.path.exists(final_folder_path):
#         os.makedirs(final_folder_path)
#     for title, data_dict in data.items():
#         file_path = os.path.join(final_folder_path, f"{title.replace(' ', '_')}.txt")
#         with open(file_path, 'w') as file:
#             for key, values in data_dict.items():
#                 file.write(f"{key}:\n")
#                 file.write("\n".join(map(str, values)) + "\n\n")
    
    plot_file_name = f"{sample_name} final evaporation plot.png"
    plot_file_path = os.path.join(final_folder_path, plot_file_name)
    plt.savefig(plot_file_path)
    print(f"All data has been saved in the folder '{final_folder_path}'")
    print('Waiting for you to close the final plot windows...')
    plt.show()
    print('Script finalized.')
    


if __name__ == "__main__":
    main()
