#!/usr/bin/env python
#
###############################################################################
#   HBLink - Copyright (C) 2020 Cortney T. Buffington, N0MJS <n0mjs@me.com>
#   GPS/Data - Copyright (C) 2020 Eric Craw, KF7EEL <kf7eel@qsl.net>
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software Foundation,
#   Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301  USA
###############################################################################

'''
This is a GPS and Data application. It decodes and reassambles DMR GPS packets and
uploads them th APRS-IS.
'''

# Python modules we need
import sys
from bitarray import bitarray
from time import time
from importlib import import_module
from types import ModuleType

# Twisted is pretty important, so I keep it separate
from twisted.internet.protocol import Factory, Protocol
from twisted.protocols.basic import NetstringReceiver
from twisted.internet import reactor, task

# Things we import from the main hblink module
from hblink import HBSYSTEM, OPENBRIDGE, systems, hblink_handler, reportFactory, REPORT_OPCODES, config_reports, mk_aliases, acl_check
from dmr_utils3.utils import bytes_3, int_id, get_alias
from dmr_utils3 import decode, bptc, const
import config
import log
import const

# The module needs logging logging, but handlers, etc. are controlled by the parent
import logging
logger = logging.getLogger(__name__)

# Other modules we need for data and GPS
from bitarray import bitarray
from binascii import b2a_hex as ahex
import re
##from binascii import a2b_hex as bhex
import aprslib
import datetime
from bitarray.util import ba2int as ba2num
from bitarray.util import ba2hex as ba2hx
import codecs
import time
#Needed for working with NMEA
import pynmea2

# Modules for executing commands/scripts
import os
from gps_functions import cmd_list

# Module for maidenhead grids
import maidenhead as mh

#Modules for APRS settings
import ast
from pathlib import Path


# Does anybody read this stuff? There's a PEP somewhere that says I should do this.
__author__     = 'Cortney T. Buffington, N0MJS; Eric Craw, KF7EEL'
__copyright__  = 'Copyright (c) 2020 Cortney T. Buffington'
__credits__    = 'Colin Durbridge, G4EML, Steve Zingman, N4IRS; Mike Zingman, N4IRR; Jonathan Naylor, G4KLX; Hans Barthen, DL5DI; Torsten Shultze, DG1HT'
__license__    = 'GNU GPLv3'
__maintainer__ = 'Eric Craw, KF7EEL'
__email__      = 'kf7eel@qsl.net'
__status__     = 'pre-alpha'

# Known to work with: AT-D878

# Must have the following at line 1054 in bridge.py to forward group vcsbk, also there is a typo there:
# self.group_received(_peer_id, _rf_src, _dst_id, _seq, _slot, _frame_type, _dtype_vseq, _stream_id, _data)

##################################################################################################

# Headers for GPS by model of radio:
# AT-D878 - Compressed UDP
# MD-380 - Unified Data Transport


# From dmr_utils3, modified to decode entire packet. Works for 1/2 rate coded data. 
def decode_full(_data):
    binlc = bitarray(endian='big')   
    binlc.extend([_data[136],_data[121],_data[106],_data[91], _data[76], _data[61], _data[46], _data[31]])
    binlc.extend([_data[152],_data[137],_data[122],_data[107],_data[92], _data[77], _data[62], _data[47], _data[32], _data[17], _data[2]  ])
    binlc.extend([_data[123],_data[108],_data[93], _data[78], _data[63], _data[48], _data[33], _data[18], _data[3],  _data[184],_data[169]])
    binlc.extend([_data[94], _data[79], _data[64], _data[49], _data[34], _data[19], _data[4],  _data[185],_data[170],_data[155],_data[140]])
    binlc.extend([_data[65], _data[50], _data[35], _data[20], _data[5],  _data[186],_data[171],_data[156],_data[141],_data[126],_data[111]])
    binlc.extend([_data[36], _data[21], _data[6],  _data[187],_data[172],_data[157],_data[142],_data[127],_data[112],_data[97], _data[82] ])
    binlc.extend([_data[7],  _data[188],_data[173],_data[158],_data[143],_data[128],_data[113],_data[98], _data[83]])
    #This is the rest of the Full LC data -- the RS1293 FEC that we don't need
    # This is extremely important for SMS and GPS though.
    binlc.extend([_data[68],_data[53],_data[174],_data[159],_data[144],_data[129],_data[114],_data[99],_data[84],_data[69],_data[54],_data[39]])
    binlc.extend([_data[24],_data[145],_data[130],_data[115],_data[100],_data[85],_data[70],_data[55],_data[40],_data[25],_data[10],_data[191]])
    return binlc
   

n_packet_assembly = 0

packet_assembly = ''

final_packet = ''

#Convert DMR packet to binary from MMDVM packet and remove Slot Type and EMB Sync stuff to allow for BPTC 196,96 decoding
def bptc_decode(_data):
        binary_packet = bitarray(decode.to_bits(_data[20:]))
        del binary_packet[98:166]
        return decode_full(binary_packet)
# Placeholder for future header id
def header_ID(_data):
    hex_hdr = str(ahex(bptc_decode(_data)))
    return hex_hdr[2:6]
    # Work in progress, used to determine data format
##    pass

def aprs_send(packet):
    AIS = aprslib.IS(aprs_callsign, passwd=aprs_passcode,host=aprs_server, port=aprs_port)
    AIS.connect()
    AIS.sendall(packet)
    AIS.close()

# Thanks for this forum post for this - https://stackoverflow.com/questions/2579535/convert-dd-decimal-degrees-to-dms-degrees-minutes-seconds-in-python

def decdeg2dms(dd):
   is_positive = dd >= 0
   dd = abs(dd)
   minutes,seconds = divmod(dd*3600,60)
   degrees,minutes = divmod(minutes,60)
   degrees = degrees if is_positive else -degrees
   return (degrees,minutes,seconds)

def user_setting_write(dmr_id, setting, value):
##    try:
    # Open file and load as dict for modification
        with open("./user_settings.txt", 'r') as f:
##            if f.read() == '{}':
##                user_dict = {}
            user_dict = ast.literal_eval(f.read())
            logger.info('Current settings: ' + str(user_dict))
            if dmr_id not in user_dict:
                user_dict[dmr_id] = [{'call': str(get_alias((dmr_id), subscriber_ids))}, {'ssid': ''}, {'icon': ''}, {'comment': ''}]

            if setting.upper() == 'ICON':
                user_dict[dmr_id][2]['icon'] = value
            if setting.upper() == 'SSID':
                user_dict[dmr_id][1]['ssid'] = value  
            if setting.upper() == 'COM':
                user_comment = user_dict[dmr_id][3]['comment'] = value[0:35]
            f.close()
            logger.info('Loaded user settings. Preparing to write...')
    # Write modified dict to file
        with open("./user_settings.txt", 'w') as user_dict_file:
            user_dict_file.write(str(user_dict))
            user_dict_file.close()
            logger.info('User setting saved')
            f.close()
            packet_assembly = ''
##    except:
##        logger.info('No data file found, creating one.')
##        #Path('./user_settings.txt').mkdir(parents=True, exist_ok=True)
##        Path('./user_settings.txt').touch()
        
##def retrieve_aprs_settings(_rf_src):
##    user_settings = ast.literal_eval(os.popen('cat ./user_settings.txt').read())
##    if int_id(_rf_src) not in user_settings:
##        aprs_loc_packet = str(get_alias(int_id(_rf_src), subscriber_ids)) + '-' + str(user_ssid) + '>APRS,TCPIP*:/' + str(datetime.datetime.utcnow().strftime("%H%M%Sh")) + str(loc.lat[0:7]) + str(loc.lat_dir) + '/' + str(loc.lon[0:8]) + str(loc.lon_dir) + '[' + str(round(loc.true_course)).zfill(3) + '/' + str(round(loc.spd_over_grnd)).zfill(3) + '/' + aprs_comment + ' DMR ID: ' + str(int_id(_rf_src))
##    else:
##        if user_settings[int_id(_rf_src)][1]['ssid'] == '':
##            ssid = user_ssid
##        if user_settings[int_id(_rf_src)][3]['comment'] == '':
##            comment = aprs_comment + ' DMR ID: ' + str(int_id(_rf_src))
##        if user_settings[int_id(_rf_src)][2]['icon'] == '':
##            icon_table = '/'
##            icon_icon = '['
##        if user_settings[int_id(_rf_src)][2]['icon'] != '':
##            icon_table = user_settings[int_id(_rf_src)][2]['icon'][0]
##            icon_icon = user_settings[int_id(_rf_src)][2]['icon'][1]
##        if user_settings[int_id(_rf_src)][1]['ssid'] != '':
##            ssid = user_settings[int_id(_rf_src)][1]['ssid']
##        if user_settings[int_id(_rf_src)][3]['comment'] != '':
##            comment = user_settings[int_id(_rf_src)][3]['comment']
##    return ssid, icon, comment
##

# Process SMS, do something bases on message

def process_sms(_rf_src, sms):
    if sms == 'ID':
        logger.info(str(get_alias(int_id(from_id), subscriber_ids)) + ' - ' + str(int_id(from_id)))
    elif sms == 'TEST':
        logger.info('It works!')
    elif '@ICON' in sms:
        user_setting_write(int_id(_rf_src), re.sub(' .*|@','',sms), re.sub('@ICON| ','',sms))
    elif '@SSID' in sms:
        user_setting_write(int_id(_rf_src), re.sub(' .*|@','',sms), re.sub('@SSID| ','',sms))
    elif '@COM' in sms:
        user_setting_write(int_id(_rf_src), re.sub(' .*|@','',sms), re.sub('@COM |@COM','',sms))
    elif '@MH' in sms:
        grid_square = re.sub('@MH ', '', sms)
        if len(grid_square) < 6:
            pass
        else:
            lat = decdeg2dms(mh.to_location(grid_square)[0])
            lon = decdeg2dms(mh.to_location(grid_square)[1])
            
            if lon[0] < 0:
                lon_dir = 'W'
            if lon[0] > 0:
                lon_dir = 'E'
            if lat[0] < 0:
                lat_dir = 'S'
            if lat[0] > 0:
                lat_dir = 'N'
            #logger.info(lat)
            #logger.info(lat_dir)
            aprs_lat = str(str(re.sub('\..*|-', '', str(lat[0]))) + str(re.sub('\..*', '', str(lat[1])) + '.').ljust(5) + lat_dir)
            aprs_lon = str(str(re.sub('\..*|-', '', str(lon[0]))) + str(re.sub('\..*', '', str(lon[1])) + '.').ljust(5) + lon_dir)
        #logger.info(mh.to_location(grid_square))
        #logger.info(str(lat) + ', ' + str(lon))
        logger.info('Latitude: ' + str(aprs_lat))
        logger.info('Longitude: ' + str(aprs_lon))
        user_settings = ast.literal_eval(os.popen('cat ./user_settings.txt').read())
        if int_id(_rf_src) not in user_settings:
            aprs_loc_packet = str(get_alias(int_id(_rf_src), subscriber_ids)) + '-' + str(user_ssid) + '>APHBL3,TCPIP*:/' + str(datetime.datetime.utcnow().strftime("%H%M%Sh")) + str(aprs_lat) + '/' + str(aprs_lon) + '[/' + aprs_comment + ' DMR ID: ' + str(int_id(_rf_src))
        else:
            if user_settings[int_id(_rf_src)][1]['ssid'] == '':
                ssid = user_ssid
            if user_settings[int_id(_rf_src)][3]['comment'] == '':
                comment = aprs_comment + ' DMR ID: ' + str(int_id(_rf_src))
            if user_settings[int_id(_rf_src)][2]['icon'] == '':
                icon_table = '/'
                icon_icon = '['
            if user_settings[int_id(_rf_src)][2]['icon'] != '':
                icon_table = user_settings[int_id(_rf_src)][2]['icon'][0]
                icon_icon = user_settings[int_id(_rf_src)][2]['icon'][1]
            if user_settings[int_id(_rf_src)][1]['ssid'] != '':
                ssid = user_settings[int_id(_rf_src)][1]['ssid']
            if user_settings[int_id(_rf_src)][3]['comment'] != '':
                comment = user_settings[int_id(_rf_src)][3]['comment']
            aprs_loc_packet = str(get_alias(int_id(_rf_src), subscriber_ids)) + '-' + ssid + '>APHBL3,TCPIP*:/' + str(datetime.datetime.utcnow().strftime("%H%M%Sh")) + str(aprs_lat) + icon_table + str(aprs_lon) + icon_icon + '/' + str(comment)
        logger.info(aprs_loc_packet)
        try:
            aprslib.parse(aprs_loc_packet)
            aprs_send(aprs_loc_packet)
        except:
            logger.info('Exception. Not uploaded')
        packet_assembly = ''
          
            
    try:
        if sms in cmd_list:
            logger.info('Executing command/script.')
            os.popen(cmd_list[sms]).read()
            packet_assembly = ''
    except:
        logger.info('Exception. Command possibly not in list, or other error.')
        packet_assembly = ''
    else:
        pass

###########

    
class DATA_SYSTEM(HBSYSTEM):
##    global n_packet_assembly, packet_assembly

    def __init__(self, _name, _config, _report):
        HBSYSTEM.__init__(self, _name, _config, _report)

    def dmrd_received(self, _peer_id, _rf_src, _dst_id, _seq, _slot, _call_type, _frame_type, _dtype_vseq, _stream_id, _data):
        # Capture data headers
        global n_packet_assembly
        #logger.info(_dtype_vseq)
        logger.info(time.strftime('%H:%M:%S - %m/%d/%y'))
        if int_id(_dst_id) == data_id:
            #logger.info(type(_seq))
            if type(_seq) is bytes:
                pckt_seq = int.from_bytes(_seq, 'big')
            else:
                pckt_seq = _seq
            # Try to classify header
            if _call_type == call_type or (_call_type == 'vcsbk' and pckt_seq > 3): #int.from_bytes(_seq, 'big') > 3 ):
                global packet_assembly
                if _dtype_vseq == 6 or _dtype_vseq == 'group':
                    global btf, hdr_start
                    hdr_start = str(header_ID(_data))
                    logger.info('Header from ' + str(get_alias(int_id(_rf_src), subscriber_ids)) + '. DMR ID: ' + str(int_id(_rf_src)))
                    logger.info(ahex(bptc_decode(_data)))
                    logger.info('Blocks to follow: ' + str(ba2num(bptc_decode(_data)[65:72])))
                    btf = ba2num(bptc_decode(_data)[65:72])
                    # Try resetting packet_assembly
                    packet_assembly = ''
                # Data blocks at 1/2 rate, see https://github.com/g4klx/MMDVM/blob/master/DMRDefines.h for data types. _dtype_seq defined here also
                if _dtype_vseq == 7:
                    btf = btf - 1
                    logger.info('Block #: ' + str(btf))
                    #logger.info(_seq)
                    logger.info('Data block from ' + str(get_alias(int_id(_rf_src), subscriber_ids)) + '. DMR ID: ' + str(int_id(_rf_src)))
                    logger.info(ahex(bptc_decode(_data)))
                    if _seq == 0:
                        n_packet_assembly = 0
                        packet_assembly = ''
                        
                    if btf < btf + 1:
                        n_packet_assembly = n_packet_assembly + 1
                        packet_assembly = packet_assembly + str(bptc_decode(_data)) #str((decode_full_lc(b_packet)).strip('bitarray('))
                    # Use block 0 as trigger. $GPRMC must also be in string to indicate NMEA.
                    # This triggers the APRS upload
                    if btf == 0:#_seq == 12:
                        final_packet = str(bitarray(re.sub("\)|\(|bitarray|'", '', packet_assembly)).tobytes().decode('utf-8', 'ignore'))
                        sms_hex = str(ba2hx(bitarray(re.sub("\)|\(|bitarray|'", '', packet_assembly))))
                        sms_hex_string = re.sub("b'|'", '', str(sms_hex))
                        #NMEA GPS sentence
                        if '$GPRMC' in final_packet:
                            logger.info(final_packet + '\n')
                            nmea_parse = re.sub('A\*.*|.*\$', '', str(final_packet))
                            loc = pynmea2.parse(nmea_parse, check=False)
                            logger.info('Latitude: ' + str(loc.lat) + str(loc.lat_dir) + ' Longitude: ' + str(loc.lon) + str(loc.lon_dir) + ' Direction: ' + str(loc.true_course) + ' Speed: ' + str(loc.spd_over_grnd) + '\n')
                            # Begin APRS format and upload
##                            aprs_loc_packet = str(get_alias(int_id(_rf_src), subscriber_ids)) + '-' + str(user_ssid) + '>APRS,TCPIP*:/' + str(datetime.datetime.utcnow().strftime("%H%M%Sh")) + str(final_packet[29:36]) + str(final_packet[39]) + '/' + str(re.sub(',', '', final_packet[41:49])) + str(final_packet[52]) + '[/' + aprs_comment + ' DMR ID: ' + str(int_id(_rf_src))
                            try:
                                # Disable opening file for reading to reduce "collision" or reading and writing at same time.
                                with open("./user_settings.txt", 'r') as f:
                                    user_settings = ast.literal_eval(f.read())
                                user_settings = ast.literal_eval(os.popen('cat ./user_settings.txt').read())
                                if int_id(_rf_src) not in user_settings:
                                    aprs_loc_packet = str(get_alias(int_id(_rf_src), subscriber_ids)) + '-' + str(user_ssid) + '>APHBL3,TCPIP*:/' + str(datetime.datetime.utcnow().strftime("%H%M%Sh")) + str(loc.lat[0:7]) + str(loc.lat_dir) + '/' + str(loc.lon[0:8]) + str(loc.lon_dir) + '[' + str(round(loc.true_course)).zfill(3) + '/' + str(round(loc.spd_over_grnd)).zfill(3) + '/' + aprs_comment + ' DMR ID: ' + str(int_id(_rf_src))
                                else:
                                    if user_settings[int_id(_rf_src)][1]['ssid'] == '':
                                        ssid = user_ssid
                                    if user_settings[int_id(_rf_src)][3]['comment'] == '':
                                        comment = aprs_comment + ' DMR ID: ' + str(int_id(_rf_src))
                                    if user_settings[int_id(_rf_src)][2]['icon'] == '':
                                        icon_table = '/'
                                        icon_icon = '['
                                    if user_settings[int_id(_rf_src)][2]['icon'] != '':
                                        icon_table = user_settings[int_id(_rf_src)][2]['icon'][0]
                                        icon_icon = user_settings[int_id(_rf_src)][2]['icon'][1]
                                    if user_settings[int_id(_rf_src)][1]['ssid'] != '':
                                        ssid = user_settings[int_id(_rf_src)][1]['ssid']
                                    if user_settings[int_id(_rf_src)][3]['comment'] != '':
                                        comment = user_settings[int_id(_rf_src)][3]['comment']
                                    #logger.info(retrieve_aprs_settings(_rf_src))
                                aprs_loc_packet = str(get_alias(int_id(_rf_src), subscriber_ids)) + '-' + ssid + '>APHBL3,TCPIP*:/' + str(datetime.datetime.utcnow().strftime("%H%M%Sh")) + str(loc.lat[0:7]) + str(loc.lat_dir) + icon_table + str(loc.lon[0:8]) + str(loc.lon_dir) + icon_icon + str(round(loc.true_course)).zfill(3) + '/' + str(round(loc.spd_over_grnd)).zfill(3) + '/' + str(comment)
                                logger.info(aprs_loc_packet)
                                logger.info('User comment: ' + comment)
                                logger.info('User SSID: ' + ssid)
                                logger.info('User icon: ' + icon_table + icon_icon)
##                                f.close()
                            except:
                                logger.info('Error or user settings file not found, proceeding with default settings.')
                                aprs_loc_packet = str(get_alias(int_id(_rf_src), subscriber_ids)) + '-' + str(user_ssid) + '>APHBL3,TCPIP*:/' + str(datetime.datetime.utcnow().strftime("%H%M%Sh")) + str(loc.lat[0:7]) + str(loc.lat_dir) + '/' + str(loc.lon[0:8]) + str(loc.lon_dir) + '[' + str(round(loc.true_course)).zfill(3) + '/' + str(round(loc.spd_over_grnd)).zfill(3) + '/' + aprs_comment + ' DMR ID: ' + str(int_id(_rf_src))
                            try:
                                # Try parse of APRS packet. If it fails, it will not upload to APRS-IS
                                aprslib.parse(aprs_loc_packet)
                                # Float values of lat and lon. Anything that is not a number will cause it to fail.
                                float(loc.lat)
                                float(loc.lon)
                                aprs_send(aprs_loc_packet)
                            except:
                                logger.info('Failed to parse packet. Packet may be deformed. Not uploaded.')
                            # Get callsign based on DMR ID
                            # End APRS-IS upload
                        # Assume this is an SMS message
                        if '$GPRMC' not in final_packet:
                            if '0005' in hdr_start:
                                logger('This may be an NMEA coded packet from an MD-380 type radio!')
                            # Revisit below later.
####                            # Motorola type SMS header
##                            if '824a' in hdr_start or '024a' in hdr_start:
##                                logger.info('\nMotorola type SMS')
##                                sms = codecs.decode(bytes.fromhex(''.join(sms_hex[74:-8].split('00'))), 'utf-8')
##                                logger.info('\n\n' + 'Received SMS from ' + str(get_alias(int_id(_rf_src), subscriber_ids)) + ', DMR ID: ' + str(int_id(_rf_src)) + ': ' + str(sms) + '\n')
##                                process_sms(_rf_src, sms)
##                                packet_assembly = ''
##                            # ETSI? type SMS header    
##                            elif '0244' in hdr_start or '8244' in hdr_start:
##                                logger.info('ETSI? type SMS')
##                                sms = codecs.decode(bytes.fromhex(''.join(sms_hex[64:-8].split('00'))), 'utf-8')
##                                logger.info('\n\n' + 'Received SMS from ' + str(get_alias(int_id(_rf_src), subscriber_ids)) + ', DMR ID: ' + str(int_id(_rf_src)) + ': ' + str(sms) + '\n')
##                                #logger.info(final_packet)
##                                #logger.info(sms_hex[64:-8])
##                                process_sms(_rf_src, sms)
##                                packet_assembly = ''
####                                
##                            else:
                                logger.info('\nSMS detected. Attempting to parse.')
                                #logger.info(final_packet)
                                logger.info(sms_hex)
##                                logger.info(type(sms_hex))
                                logger.info('Attempting to find command...')
##                                sms = codecs.decode(bytes.fromhex(''.join(sms_hex[:-8].split('00'))), 'utf-8', 'ignore')
                                sms = codecs.decode(bytes.fromhex(''.join(sms_hex_string[:-8].split('00'))), 'utf-8', 'ignore')
                                msg_found = re.sub('.*\n', '', sms)
                                logger.info('\n\n' + 'Received SMS from ' + str(get_alias(int_id(_rf_src), subscriber_ids)) + ', DMR ID: ' + str(int_id(_rf_src)) + ': ' + str(msg_found) + '\n')
                                process_sms(_rf_src, msg_found)
                                packet_assembly = ''
                                pass
                                #logger.info(bitarray(re.sub("\)|\(|bitarray|'", '', str(bptc_decode(_data)).tobytes().decode('utf-8', 'ignore'))))
                            #logger.info('\n\n' + 'Received SMS from ' + str(get_alias(int_id(_rf_src), subscriber_ids)) + ', DMR ID: ' + str(int_id(_rf_src)) + ': ' + str(sms) + '\n')
                        # Reset the packet assembly to prevent old data from returning.
                        hdr_start = ''
                    #logger.info(_seq)
                    #packet_assembly = '' #logger.info(_dtype_vseq)
                #logger.info(ahex(bptc_decode(_data)).decode('utf-8', 'ignore'))
                #logger.info(bitarray(re.sub("\)|\(|bitarray|'", '', str(bptc_decode(_data)).tobytes().decode('utf-8', 'ignore'))))

        else:
            pass


#************************************************
#      MAIN PROGRAM LOOP STARTS HERE
#************************************************

if __name__ == '__main__':
    #global aprs_callsign, aprs_passcode, aprs_server, aprs_port, user_ssid, aprs_comment, call_type, data_id
    import argparse
    import sys
    import os
    import signal
    from dmr_utils3.utils import try_download, mk_id_dict

    # Change the current directory to the location of the application
    os.chdir(os.path.dirname(os.path.realpath(sys.argv[0])))

    # Check if user_settings (for APRS settings of users) exists. Creat it if not.
    if Path('./user_settings.txt').is_file():
        pass
    else:
        Path('./user_settings.txt').touch()
        with open("./user_settings.txt", 'w') as user_dict_file:
            user_dict_file.write("{1: [{'call': 'N0CALL'}, {'ssid': ''}, {'icon': ''}, {'comment': ''}]}")
            user_dict_file.close()

    # CLI argument parser - handles picking up the config file from the command line, and sending a "help" message
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', action='store', dest='CONFIG_FILE', help='/full/path/to/config.file (usually gps_data.cfg)')
    parser.add_argument('-l', '--logging', action='store', dest='LOG_LEVEL', help='Override config file logging level.')
    cli_args = parser.parse_args()

    # Ensure we have a path for the config file, if one wasn't specified, then use the default (top of file)
    if not cli_args.CONFIG_FILE:
        cli_args.CONFIG_FILE = os.path.dirname(os.path.abspath(__file__))+'/gps_data.cfg'

    # Call the external routine to build the configuration dictionary
    CONFIG = config.build_config(cli_args.CONFIG_FILE)

    data_id = int(CONFIG['GPS_DATA']['DATA_DMR_ID'])

    # Group call or Unit (private) call
    call_type = CONFIG['GPS_DATA']['CALL_TYPE']
    # APRS-IS login information
    aprs_callsign = CONFIG['GPS_DATA']['APRS_LOGIN_CALL']
    aprs_passcode = int(CONFIG['GPS_DATA']['APRS_LOGIN_PASSCODE'])
    aprs_server = CONFIG['GPS_DATA']['APRS_SERVER']
    aprs_port = int(CONFIG['GPS_DATA']['APRS_PORT'])
    user_ssid = CONFIG['GPS_DATA']['USER_APRS_SSID']
    aprs_comment = CONFIG['GPS_DATA']['USER_APRS_COMMENT']

    # Start the system logger
    if cli_args.LOG_LEVEL:
        CONFIG['LOGGER']['LOG_LEVEL'] = cli_args.LOG_LEVEL
    logger = log.config_logging(CONFIG['LOGGER'])
    logger.info('\n\nCopyright (c) 2013, 2014, 2015, 2016, 2018, 2019\n\tThe Regents of the K0USY Group. All rights reserved.\n GPS and Data decoding by Eric, KF7EEL')
    logger.debug('Logging system started, anything from here on gets logged')

    # Set up the signal handler
    def sig_handler(_signal, _frame):
        logger.info('SHUTDOWN: >>>GPS and Data Decoder<<< IS TERMINATING WITH SIGNAL %s', str(_signal))
        hblink_handler(_signal, _frame)
        logger.info('SHUTDOWN: ALL SYSTEM HANDLERS EXECUTED - STOPPING REACTOR')
        reactor.stop()

    # Set signal handers so that we can gracefully exit if need be
    for sig in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, sig_handler)

    # Create the name-number mapping dictionaries
    peer_ids, subscriber_ids, talkgroup_ids = mk_aliases(CONFIG)
    
    
    # INITIALIZE THE REPORTING LOOP
    if CONFIG['REPORTS']['REPORT']:
        report_server = config_reports(CONFIG, reportFactory)
    else:
        report_server = None
        logger.info('(REPORT) TCP Socket reporting not configured')

    # HBlink instance creation
    logger.info('HBlink \'gps_data.py\' -- SYSTEM STARTING...')
    for system in CONFIG['SYSTEMS']:
        if CONFIG['SYSTEMS'][system]['ENABLED']:
            if CONFIG['SYSTEMS'][system]['MODE'] == 'OPENBRIDGE':
                systems[system] = OPENBRIDGE(system, CONFIG, report_server)
            else:
                systems[system] = DATA_SYSTEM(system, CONFIG, report_server)
                
            reactor.listenUDP(CONFIG['SYSTEMS'][system]['PORT'], systems[system], interface=CONFIG['SYSTEMS'][system]['IP'])
            logger.debug('%s instance created: %s, %s', CONFIG['SYSTEMS'][system]['MODE'], system, systems[system])

    reactor.run()

    
# John 3:16 - For God so loved the world, that he gave his only Son,
# that whoever believes in him should not perish but have eternal life.
