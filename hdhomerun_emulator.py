import socket
import threading
import time
import logging
import select
import struct

logger = logging.getLogger(__name__)

class HDHomeRunEmulator:
    def __init__(self, http_port=5005):
        self.http_port = http_port
        self.device_id = "12345678"
        self.model = "HDTC-2US"
        self.friendly_name = "IPTV HDHomeRun"
        self.tuner_count = 2
        self.running = False
        self.thread = None
    
    def get_host_ip(self):
        """Get the host IP that Plex can reach"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            logger.info(f"Detected host IP: {ip}")
            return ip
        except Exception as e:
            logger.error(f"Error getting host IP: {e}")
            return "127.0.0.1"
    
    def create_ssdp_response(self):
        host_ip = self.get_host_ip()
        base_url = f"http://{host_ip}:{self.http_port}"
        
        response = f"""HTTP/1.1 200 OK
CACHE-CONTROL: max-age=1800
EXT:
LOCATION: {base_url}/discover.json
SERVER: HDHomeRun/1.0 UPnP/1.0
ST: upnp:rootdevice
USN: uuid:{self.device_id}::upnp:rootdevice
BOOTID.UPNP.ORG: 1
CONFIGID.UPNP.ORG: 1
DEVICEID.UPNP.ORG: {self.device_id}
HDHomerun-Device: {self.device_id}
HDHomerun-Device-Auth: iptv_emulator
HDHomerun-Features: base

"""
        return response
    
    def handle_ssdp_discovery(self, data, addr, sock):
        if "M-SEARCH" in data:
            logger.info(f"SSDP discovery from {addr}")
            
            if any(st in data for st in ["upnp:rootdevice", "ssdp:all", "urn:schemas-upnp-org:device:MediaRenderer:1"]):
                response = self.create_ssdp_response()
                try:
                    sock.sendto(response.encode('utf-8'), addr)
                    logger.info(f"Sent SSDP response to {addr}")
                except Exception as e:
                    logger.error(f"Error sending SSDP response: {e}")
    
    def run_ssdp_server(self):
        """Run SSDP discovery server in background thread"""
        self.running = True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.bind(('0.0.0.0', 1900))
            
            mreq = struct.pack("4sl", socket.inet_aton("239.255.255.250"), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            
            sock.setblocking(0)
            
            logger.info("HDHomeRun SSDP server started on port 1900")
            
            while self.running:
                try:
                    ready = select.select([sock], [], [], 1.0)
                    if ready[0]:
                        data, addr = sock.recvfrom(1024)
                        self.handle_ssdp_discovery(data.decode('utf-8'), addr, sock)
                except Exception as e:
                    if self.running:
                        logger.error(f"SSDP error: {e}")
                    time.sleep(1)
                    
        except Exception as e:
            logger.error(f"SSDP server failed: {e}")
        finally:
            if 'sock' in locals():
                sock.close()
            logger.info("SSDP Server stopped")
    
    def start(self):
        """Start the SSDP server in a background thread"""
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.run_ssdp_server, daemon=True)
            self.thread.start()
            logger.info("HDHomeRun emulator background thread started")

# Create instance but DON'T start it here
hdhomerun_emulator = HDHomeRunEmulator()
