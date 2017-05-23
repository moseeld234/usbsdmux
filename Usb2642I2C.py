#!/usr/bin/env python3

import struct
import ctypes
import string
import fcntl

class FrameLengthException(Exception):
  pass

class IoctlFailed(Exception):
  pass

class I2cTransactionFailed(Exception):
  pass

class Usb2642I2C(object):
  """
  This class provides an interface to interact with devices on a Microchip USB2642 auxiliary I2C Bus.

  To do so it uses vendor specific SCSI-commands on the mass-storage device provided by the USB2642.
  Documentation to this behavior can befond in this documents:

  * 'Microchip: I2C_Over_USB_UserGuilde_50002283A.pdf' (Can be found in the (Windows-) software example provided on the components webpage)
  * The USB2641 datasheet
  * see http://www.microchip.com/wwwproducts/en/USB2642 for both documents

  Some more interesting links:

  * USB Mass Storage Bulk Transfer Profile Specification: http://www.usb.org/developers/docs/devclass_docs/usbmassbulk_10.pdf
  * Linux SG_IO ioctl() control structure: http://www.tldp.org/HOWTO/SCSI-Generic-HOWTO/sg_io_hdr_t.html
  * Denton Gentry's blog post about how to use the sg ioctl() from python: http://codingrelic.geekhold.com/2012/02/ata-commands-in-python.html This article uses it to make ATA-passthrough - beware that we do not use ATA-passthrough here.


  This class uses the /dev/sg* -Interface to access the SCSI-device even if no media is present.
  Make sure you have rw-rights on it :)
  """

  class _SgioHdrStruct(ctypes.Structure):
    """
    Structure used to access the ioctl() to send arbitrary SCSI-commands.

    Reflects the Kernel-Struct from:
    <scsi/sg.h> sg_io_hdr_t.
    """

    _fields_ = [
        ('interface_id', ctypes.c_int),
        ('dxfer_direction', ctypes.c_int),
        ('cmd_len', ctypes.c_ubyte),
        ('mx_sb_len', ctypes.c_ubyte),
        ('iovec_count', ctypes.c_ushort),
        ('dxfer_len', ctypes.c_uint),
        ('dxferp', ctypes.c_void_p),
        ('cmdp', ctypes.c_void_p),
        ('sbp', ctypes.c_void_p),
        ('timeout', ctypes.c_uint),
        ('flags', ctypes.c_uint),
        ('pack_id', ctypes.c_int),
        ('usr_ptr', ctypes.c_void_p),
        ('status', ctypes.c_ubyte),
        ('masked_status', ctypes.c_ubyte),
        ('msg_status', ctypes.c_ubyte),
        ('sb_len_wr', ctypes.c_ubyte),
        ('host_status', ctypes.c_ushort),
        ('driver_status', ctypes.c_ushort),
        ('resid', ctypes.c_int),
        ('duration', ctypes.c_uint),
        ('info', ctypes.c_uint)]

  """IOCTL for SG_IO"""
  _SG_IO = 0x2285  # <scsi/sg.h>

  """SgioHdr dxfer direction constant: No direction"""
  _SG_DXFER_NONE = -1

  """SgioHdr dxfer direction constant: Host to device"""
  _SG_DXFER_TO_DEV = -2

  """SgioHdr dxfer direction constant: Device to Host"""
  _SG_DXFER_FROM_DEV = -3

  """
  This Opcode represents a vendor specific SCSI command.
  According to: 'Microchip: I2C_Over_USB_UserGuilde_50002283A.pdf' P.20
  """
  _USB2642SCSIOPCODE = 0xCF

  """
  This Vendor Action marks an I2C Write Action
  According to: 'Microchip: I2C_Over_USB_UserGuilde_50002283A.pdf' P.20
  """
  _USB2642I2CWRITESTREAM = 0x23

  """
  This Vendor Action marks an I2C Write-Read Action
  According to: 'Microchip: I2C_Over_USB_UserGuilde_50002283A.pdf' P.20
  """
  _USB2642I2CWRITEREADSTREAM = 0x22

  class _USB2642I2cWriteStruct(ctypes.Structure):
    """I2C-Write Data Structure for up to 512 Bytes of Data


    According to: 'Microchip: I2C_Over_USB_UserGuilde_50002283A.pdf' P.20
    """

    _fields_ = [
      ("ScsiVendorCommand", ctypes.c_uint8),
      ("ScsiVendorActionWriteI2C", ctypes.c_uint8),
      ("I2cSlaveAddress", ctypes.c_uint8),
      ("I2cUnused", ctypes.c_uint8),
      ("I2cDataPhaseLenHigh", ctypes.c_uint8),
      ("I2cDataPhaseLenLow", ctypes.c_uint8),
      ("I2cCommandPhaseLen", ctypes.c_uint8),
      ("I2cCommandPayload", ctypes.c_uint8 * 9)
    ]

  class _USB2642I2cReadStruct(ctypes.Structure):
    """
    I2C-Read Data Structure for up to 512 Bytes of Data.

    According to: 'Microchip: I2C_Over_USB_UserGuilde_50002283A.pdf' P.20
    """

    _fields_ = [
      ("ScsiVendorCommand", ctypes.c_uint8),
      ("ScsiVendorActionWriteReadI2C", ctypes.c_uint8),
      ("I2cWriteSlaveAddress", ctypes.c_uint8),
      ("I2cReadSlaveAddress", ctypes.c_uint8),
      ("I2cReadPhaseLenHigh", ctypes.c_uint8),
      ("I2cReadPhaseLenLow", ctypes.c_uint8),
      ("I2cWritePhaseLen", ctypes.c_uint8),
      ("I2cWritePayload", ctypes.c_uint8 * 9)
    ]

  def toPrettyHexString(self, buffer):
    """Takes a byte-buffer and creates a pretty-looking hex-string from it"""

    if isinstance(buffer, ctypes.Structure):
      out = ctypes.c_buffer(ctypes.sizeof(buffer))
      ctypes.memmove(ctypes.addressof(out), ctypes.addressof(buffer), ctypes.sizeof(buffer))
      b = [ord(x) for x in out]
    elif isinstance(buffer[0], int):
      b = [x for x in buffer]
    else:
      b = [ord(x) for x in buffer]

    res = ""
    offs = 0
    while len(b)>0:
      slice = b[0:8]
      b = b[8:]
      res += "0x{:02X}\t{}  {}\n".format(offs, " ".join(["{:02X}".format(x) for x in slice]), " ".join([chr(x) if chr(x) in string.printable.split(" ")[0] else "." for x in slice]))
      offs += 8
    return res


  def _getScsiCmdI2cWrite(self, slaveAddr, data):
    """
    Create an I2cWrite Command Structure to write up to 512 bytes to device slaveAddr.

    According to: 'Microchip: I2C_Over_USB_UserGuilde_50002283A.pdf' P.20
    """

    MAXLEN = 512
    count = len(data) if len(data) <= MAXLEN else MAXLEN
    dataArray = (ctypes.c_uint8 * MAXLEN)()
    for i in range(count):
      dataArray[i] = data[i]

    slaveWriteAddr = (slaveAddr*2)&0xFF

    cmd = self._USB2642I2cWriteStruct(
      ScsiVendorCommand = self._USB2642SCSIOPCODE,
      ScsiVendorActionWriteI2C = self._USB2642I2CWRITESTREAM,
      I2cSlaveAddress = slaveWriteAddr,
      I2cUnused = 0x00,
      I2cDataPhaseLenHigh = (count>>8)&0xFF,
      I2cDataPhaseLenLow  = count&0xFF,
      I2cCommandPhaseLen = 0x00,
      I2cCommandPayload = (ctypes.c_uint8*9)())

    return cmd, dataArray

  def _getScsiCmdI2cWriteRead(self, slaveAddr, writeData, readLength):
    """
    Create an I2cWriteRead Command Structure to write up to 9 bytes to device slaveAddr and then read back up to 512 bytes of data.

    According to: 'Microchip: I2C_Over_USB_UserGuilde_50002283A.pdf' P.20
    """

    MAXLEN = 512
    readCount = readLength if readLength <= MAXLEN else MAXLEN
    readDataArray = (ctypes.c_uint8 * MAXLEN)()

    MAXLEN = 9
    writeCount = len(writeData) if len(writeData) <= MAXLEN else MAXLEN
    writeDataArray = (ctypes.c_uint8 * MAXLEN)()
    for i in range(min(MAXLEN, len(writeData))):
      writeDataArray[i] = writeData[i]

    slaveWriteAddr = (slaveAddr*2)&0xFF
    slaveReadAddr = slaveWriteAddr + 1

    cmd = self._USB2642I2cReadStruct(
      ScsiVendorCommand = self._USB2642SCSIOPCODE,
      ScsiVendorActionWriteReadI2C = self._USB2642I2CWRITEREADSTREAM,
      I2cWriteSlaveAddress = slaveWriteAddr,
      I2cReadSlaveAddress = slaveReadAddr,
      I2cReadPhaseLenHigh = (readCount>>8)&0xFF,
      I2cReadPhaseLenLow  = readCount&0xFF,
      I2cWritePhaseLen = writeCount,
      I2cCommandPayload = writeDataArray)

    return cmd, readDataArray


  def _getSgio(self, command, Sg_Dxfer, databuffer):
    """Fill the SG_IO ioctl() -structure with sane defaults for the given command.
    The command will create a 64-byte sense-buffer for returned status."""
    ASCII_S = 83
    sense = ctypes.c_buffer(64)

    sgio = self._SgioHdrStruct(
                  # "S" for SCSI
                  interface_id=ASCII_S,
                  # SG_DXFER_*
                  dxfer_direction=Sg_Dxfer,
                  # length of whatever we put into cmd
                  cmd_len=ctypes.sizeof(command),
                  # length of sense buffer
                  mx_sb_len=ctypes.sizeof(sense), iovec_count=0,
                  # data transfer length
                  dxfer_len=ctypes.sizeof(databuffer),
                  # pointer to data transfer buffer
                  dxferp=ctypes.cast(databuffer, ctypes.c_void_p),
                  # command to perform
                  cmdp=ctypes.addressof(command),
                  # sense buffer memory
                  sbp=ctypes.cast(sense, ctypes.c_void_p),
                  # a timeout for this command in ms
                  timeout=3000,
                  # SG_FLAG_*, normally 0
                  flags=0,
                  # unused
                  pack_id=0,
                  # unused
                  usr_ptr=None,
                  #output: SCSI-status
                  status=0,
                  #output: shifted, maskes SCSI-stauts
                  masked_status=0,
                  #output: optional: message level data
                  msg_status=0,
                  #output: byte actually written to sbp
                  sb_len_wr=0,
                  # output: errors from host adapter
                  host_status=0,
                  #output: errors from software driver
                  driver_status=0,
                  # output: result_len: actually transferred data
                  resid=0,
                  #output: time for the command in ms
                  duration=0,
                  # output: auxiliary information (?)
                  info=0)
    return sgio, sense


  def _callIoctl(self, command, sg_dxfer, databuffer):
    """
    Call the ioctl()

    This function will create the struct to call the ioctl() and handle return codes.
    """
    sgio, sense = self._getSgio(command, sg_dxfer, databuffer)
#    print("SGIO:")
#    print(self.toPrettyHexString(sgio))

    with open(self.sg, 'r') as fh:
      rc =  fcntl.ioctl(fh, self._SG_IO, ctypes.addressof(sgio))
      if rc != 0:
        raise IoctlFailed("SG_IO ioctl() failed with non-zero exit-code {}".format(rc))
    return databuffer, sense, sgio


  def writeReadTo(self, i2cAddr, writeData, readLength):
    """
    Tries to write data to an I2C-Device and afterwards read data from that device.

    This function will perform am I2C-Transaction like the following:

    * I2C-Start
    * I2C-Slave address with R/W = W (0)
    * writeData[0]
    * writeData[1]
    * ...
    * I2C-Repeated Start
    * I2C-Slave address wit R/W = R (1)
    * readData[0]
    * readData[1]
    * ...
    * I2C-Stop

    This transaction can (for example) be used to set the address-pointer inside an EEPROM and read data from it.

    Arguments:
    i2cAddr -- 7-Bit I2C Slave address (as used by Linux). Will be shifted 1 Bit to the left before adding the R/W-bit.
    writeData -- iterable of bytes to write in the first phase
    readLengh -- number of bytes (0..512) to read in the second phase
    """
    scsiCommand, data = self._getScsiCmdI2cWriteRead(i2cAddr, writeData, readLength)
    # TODO: Add error handling if length of read or write do not match requirements

#    print("I2C-Command:")
#    print(self.toPrettyHexString(scsiCommand))
#    print("I2C-Payload:")
#    print(self.toPrettyHexString(data))
    data, sense, sgio = self._callIoctl(scsiCommand, self._SG_DXFER_FROM_DEV, data)

    if sgio.status != 0:
      raise I2cTransactionFailed("SCSI-Transaction ended with status {}. I2C-Transaction has probably failed.".format(sgio.status))

    ret = []
    for i in range(min(len(data), readLength)):
      ret.append(data[i])

    return ret

  def writeTo(self, i2cAddr, data):
    """
    Tries to write data to an I2C-Device.

    This function will perform am I2C-Transaction like the following:

    * I2C-Start
    * I2C-Slave address with R/W = W (0)
    * data[0]
    * data[1]
    * ...
    * I2C-Stop

    Transactions like this can (for example) be used if configuration registers on a device have to be written.

    Arguments:
    i2cAddr -- 7-Bit I2C Slave address (as used by Linux). Will be shifted 1 Bit to the left before adding the R/W-bit.
    data -- iterateable of bytes to write."""
    scsiCommand, data = self._getScsiCmdI2cWrite(i2cAddr, data)
    # TODO: Add length checks

#    print("I2C-Command:")
#    print(self.toPrettyHexString(scsiCommand))
#    print("I2C-Payload:")
#    print(self.toPrettyHexString(data))
    data, sense, sgio = self._callIoctl(scsiCommand, self._SG_DXFER_TO_DEV, data)

    if sgio.status != 0:
      raise I2cTransactionFailed("SCSI-Transaction ended with status {}. I2C-Transaction has probably failed.".format(sgio.status))


  def __init__(self, sg):
    """
    Create a new USB2642I2C-Interface wrapper.

    Arguments:
    sg -- The sg-device to use. E.g. "/dev/sg1"
    """
    self.sg = sg


