from types import SimpleNamespace
from modules.proxmox_client import normalise_node
from modules.k3s_client import node_ready,evaluate_pod
from modules.docker_client import evaluate_container
def test_proxmox_normalisation():
    n=normalise_node({'node':'pve','status':'online','cpu':.42,'maxcpu':8,'mem':50,'maxmem':100});assert n['cpu_percent']==42.0 and n['ram_percent']==50.0 and n['cpu_cores']==8
def test_proxmox_zero_ram():assert normalise_node({'maxmem':0})['ram_percent']==0
def test_k3s_health():
    node=SimpleNamespace(status=SimpleNamespace(conditions=[SimpleNamespace(type='Ready',status='True')]));assert node_ready(node)
    cs=SimpleNamespace(ready=False,restart_count=4,state=SimpleNamespace(waiting=SimpleNamespace(reason='CrashLoopBackOff')))
    pod=SimpleNamespace(status=SimpleNamespace(container_statuses=[cs],phase='Running'));assert evaluate_pod(pod)=='crashloop'
def test_docker_evaluation():
    status,health,restarts,unexpected=evaluate_container({'State':{'Status':'running','Health':{'Status':'unhealthy'}},'RestartCount':3},1);assert (status,health,restarts,unexpected)==('unhealthy','unhealthy',3,True)
