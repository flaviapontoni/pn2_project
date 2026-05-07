import time
from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.util import dpid_to_str
from pox.lib.addresses import IPAddr
from pox.lib.recoco import Timer

log = core.getLogger()

class MLRoutingApp(object):
    def __init__(self):
        core.openflow.addListeners(self)
        core.openflow_discovery.addListeners(self)
        
        self.graph = {}
        self.mac_to_location = {}
        
        # --- PROJECT VARIABLES ---
        self.collector_ip = IPAddr("10.0.0.100")
        self.collector_port = 5000
        
        self.active_workers = set()
        self.worker_stats = {} 
        self.training_start_time = time.time()  # t=0 to calculate Phase
        
        Timer(3, self._request_stats, recurring=True)
        
        log.info("ML Routing Controller: COMPLETE PROJECT ACTIVE (Discovery, Stats, ECMP)")

    def _handle_ConnectionUp(self, event):
        dpid = event.dpid
        if dpid not in self.graph:
            self.graph[dpid] = {}

    def _handle_LinkEvent(self, event):
        link = event.link
        if event.added:
            self.graph.setdefault(link.dpid1, {})[link.dpid2] = link.port1
            self.graph.setdefault(link.dpid2, {})[link.dpid1] = link.port2
        elif event.removed:
            if link.dpid2 in self.graph.get(link.dpid1, {}):
                del self.graph[link.dpid1][link.dpid2]
            if link.dpid1 in self.graph.get(link.dpid2, {}):
                del self.graph[link.dpid2][link.dpid1]

    # ==========================================
    # PHASE 3: ECMP ALGORITHM (Load Balancing)
    # ==========================================
    def get_balanced_path(self, src_dpid, dst_dpid, src_identifier):
        if src_dpid == dst_dpid:
            return [src_dpid]
        if src_dpid not in self.graph or dst_dpid not in self.graph:
            return None

        queue = [[src_dpid]]
        shortest_paths = []
        shortest_len = float('inf')

        while queue:
            path = queue.pop(0)
            if len(path) > shortest_len:
                break

            node = path[-1]
            if node == dst_dpid:
                shortest_paths.append(path)
                shortest_len = len(path)
                continue

            for neighbor in self.graph.get(node, {}):
                if neighbor not in path:
                    queue.append(path + [neighbor])

        if not shortest_paths:
            return None

        hash_val = hash(str(src_identifier))
        chosen_path = shortest_paths[hash_val % len(shortest_paths)]
        return chosen_path

    # ==========================================
    # PHASE 2: STATISTICS (K_v, D_v, T_v, Phi_v)
    # ==========================================
    def _request_stats(self):
        for connection in core.openflow.connections:
            req = of.ofp_stats_request(body=of.ofp_flow_stats_request())
            connection.send(req)

    def _handle_FlowStatsReceived(self, event):
        for stat in event.stats:
            match = stat.match
            if match.dl_type == 0x0800 and match.nw_proto == 6:
                if match.nw_dst == self.collector_ip and match.tp_dst == self.collector_port:
                    worker_ip = str(match.nw_src)
                    current_bytes = stat.byte_count
                    now = time.time()
                    
                    if worker_ip not in self.worker_stats:
                        self.worker_stats[worker_ip] = {
                            'last_bytes': current_bytes, 
                            'last_time': now,
                            'phi_v': None  # Store the phase
                        }
                    else:
                        last_bytes = self.worker_stats[worker_ip]['last_bytes']
                        last_time = self.worker_stats[worker_ip]['last_time']
                        delta_bytes = current_bytes - last_bytes
                        
                        if delta_bytes > 50000:  # If bytes sent exceed 50KB = it's a BURST
                            delta_time = now - last_time
                            
                            # If this is the worker's first burst, calculate the phase
                            if self.worker_stats[worker_ip]['phi_v'] is None:
                                self.worker_stats[worker_ip]['phi_v'] = now - self.training_start_time
                            
                            Kv = len(self.active_workers)
                            Dv_MB = delta_bytes / 1000000.0
                            Tv_sec = delta_time
                            Phi_v = self.worker_stats[worker_ip]['phi_v']
                            
                            log.info(f" TRAINING | K_v: {Kv} | IP: {worker_ip} | D_v: {Dv_MB:.2f} MB | T_v: {Tv_sec:.1f} s | Phi_v: {Phi_v:.1f} s")
                            
                            self.worker_stats[worker_ip]['last_time'] = now
                            
                        self.worker_stats[worker_ip]['last_bytes'] = current_bytes

    # ==========================================
    # PHASE 1: PACKET HANDLING (Discovery)
    # ==========================================
    def _handle_PacketIn(self, event):
        packet = event.parsed
        if not packet.parsed:
            return

        dpid = event.dpid
        in_port = event.port
        src_mac = packet.src
        dst_mac = packet.dst

        is_switch_link = False
        for neighbor_dpid in self.graph.get(dpid, {}):
            if self.graph[dpid][neighbor_dpid] == in_port:
                is_switch_link = True
                break
        
        if not is_switch_link and not src_mac.is_multicast:
            if src_mac not in self.mac_to_location or self.mac_to_location[src_mac] != (dpid, in_port):
                self.mac_to_location[src_mac] = (dpid, in_port)

        ipv4_packet = packet.find('ipv4')
        tcp_packet = packet.find('tcp') if ipv4_packet else None
        
        if ipv4_packet and tcp_packet:
            if ipv4_packet.dstip == self.collector_ip and tcp_packet.dstport == self.collector_port:
                worker_ip = ipv4_packet.srcip
                if worker_ip not in self.active_workers:
                    self.active_workers.add(worker_ip)
                    # Reset time t=0 when we discover the first worker, so Phases are precise!
                    if len(self.active_workers) == 1:
                        self.training_start_time = time.time()
                    log.info(f" WORKER {worker_ip} DISCOVERED. K_v increases to: {len(self.active_workers)}")

        if dst_mac.is_multicast:
            self.flood_to_hosts(event)
            return

        if dst_mac in self.mac_to_location:
            dst_dpid, dst_port = self.mac_to_location[dst_mac]
            
            identifier = ipv4_packet.srcip if ipv4_packet else src_mac
            path = self.get_balanced_path(dpid, dst_dpid, identifier)
            
            if path is None:
                self.flood_to_hosts(event)
                return
            
            for i in range(len(path)):
                current_node = path[i]
                if current_node == dst_dpid:
                    out_port_for_switch = dst_port
                else:
                    next_node = path[i+1]
                    out_port_for_switch = self.graph[current_node][next_node]
                
                msg = of.ofp_flow_mod()
                msg.match.dl_type = packet.type
                msg.match.dl_src = src_mac
                msg.match.dl_dst = dst_mac
                if ipv4_packet:
                    msg.match.nw_src = ipv4_packet.srcip
                    msg.match.nw_dst = ipv4_packet.dstip
                    if tcp_packet:
                        msg.match.nw_proto = 6
                        msg.match.tp_src = tcp_packet.srcport
                        msg.match.tp_dst = tcp_packet.dstport

                msg.actions.append(of.ofp_action_output(port=out_port_for_switch))
                msg.idle_timeout = 30
                msg.hard_timeout = 120
                
                conn = core.openflow.getConnection(current_node)
                if conn:
                    conn.send(msg)
            
            if dpid == dst_dpid:
                out_port = dst_port
            else:
                out_port = self.graph[dpid][path[1]]
                
            msg_out = of.ofp_packet_out()
            msg_out.data = event.ofp
            msg_out.in_port = in_port
            msg_out.actions.append(of.ofp_action_output(port=out_port))
            event.connection.send(msg_out)
        else:
            self.flood_to_hosts(event)

    def flood_to_hosts(self, event):
        raw_packet = event.parsed.pack()
        for connection in core.openflow.connections:
            dpid = connection.dpid
            out_ports = []
            for port_no in connection.ports.keys():
                if port_no > of.OFPP_MAX:
                    continue
                is_switch_link = False
                for neighbor in self.graph.get(dpid, {}):
                    if self.graph[dpid][neighbor] == port_no:
                        is_switch_link = True
                        break
                if is_switch_link:
                    continue
                if dpid == event.dpid and port_no == event.port:
                    continue
                out_ports.append(port_no)
            
            if out_ports:
                msg = of.ofp_packet_out()
                msg.data = raw_packet
                msg.buffer_id = None
                msg.in_port = of.OFPP_NONE
                for p in out_ports:
                    msg.actions.append(of.ofp_action_output(port=p))
                connection.send(msg)

def launch():
    core.registerNew(MLRoutingApp)