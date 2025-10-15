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
        self._stop_event = threading.Event()
    
    def get_host_ip(self):
        """Simple IP detection"""
        try:
            # Use a short timeout so this call can't block for long on networks
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            try:
                # The connect here doesn't perform network I/O for UDP, but it
                # can still block on some platforms; setting a timeout keeps us safe.
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
            except Exception:
                ip = "127.0.0.1"
            finally:
                try:
                    s.close()
                except Exception:
                    pass
            return ip
        except Exception as e:
            logger.debug(f"get_host_ip failed: {e}")
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
            if any(st in data for st in ["upnp:rootdevice", "ssdp:all", "urn:schemas-upnp-org:device:MediaRenderer:1"]):
                response = self.create_ssdp_response()
                try:
                    sock.sendto(response.encode('utf-8'), addr)
                except Exception as e:
                    pass
    
    def run_ssdp_server(self):
        self.running = True
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            try:
                t0 = time.time()
                sock.bind(('0.0.0.0', 1900))
                t1 = time.time()
                logger.info(f"SSDP socket.bind completed in {t1 - t0:.3f}s")
            except Exception as e:
                logger.error(f"SSDP bind failed: {e}")
                raise

            mreq = struct.pack("4sl", socket.inet_aton("239.255.255.250"), socket.INADDR_ANY)
            try:
                t0 = time.time()
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                t1 = time.time()
                logger.info(f"SSDP setsockopt ADD_MEMBERSHIP completed in {t1 - t0:.3f}s")
            except Exception as e:
                logger.error(f"SSDP setsockopt ADD_MEMBERSHIP failed: {e}")
                # Not fatal; continue but log

            sock.setblocking(0)

            while self.running:
                try:
                    ready = select.select([sock], [], [], 1.0)
                    if ready[0]:
                        data, addr = sock.recvfrom(1024)
                        self.handle_ssdp_discovery(data.decode('utf-8'), addr, sock)
                except Exception:
                    logger.debug("SSDP listen error")
                    time.sleep(1)
        except Exception as e:
            logger.error(f"SSDP server failed: {e}")
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    
    def start(self):
        if self.thread is None or not self.thread.is_alive():
            logger.info("Starting SSDP thread for HDHomeRun emulator")
            self.thread = threading.Thread(target=self.run_ssdp_server, daemon=True)
            self.thread.start()

    def stop(self, timeout: float = 2.0):
        """Stop the SSDP thread gracefully."""
        try:
            logger.info("Stopping SSDP thread for HDHomeRun emulator")
            self.running = False
            if self.thread is not None and self.thread.is_alive():
                self.thread.join(timeout)
        except Exception as e:
            logger.error(f"Error stopping SSDP thread: {e}")

    def is_running(self) -> bool:
        """Check if the emulator is running by verifying both the thread state and running flag."""
        return bool(self.running and self.thread and self.thread.is_alive())

hdhomerun_emulator = HDHomeRunEmulator()
