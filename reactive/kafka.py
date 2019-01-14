import os
import glob
import shutil
import tarfile

from path import Path
from subprocess import check_call, CalledProcessError
from charmhelpers import fetch
from charmhelpers.core.hookenv import (
    resource_get, 
    charm_dir, 
    config, 
    log, 
    open_port,
)
from charmhelpers.core.templating import render
from charms.reactive import (
    when, 
    when_not, 
    set_flag, 
    endpoint_from_flag, 
    is_flag_set,
)
from jujubigdata import utils
from charms.layer import status


@when_not('zookeeper.joined')
def waiting_for_zookeeper():
    status.blocked('Waiting for Zookeeper relation')


@when('zookeeper.joined',
      'zookeeper.ready')
@when_not('kafka.installed')
def install_kafka():
    status.maintenance('Installing Kafka')

    # Check if mimimum amount of brokers are available
    min_brokers = config().get('broker-count')
    broker_count = 1
    if min_brokers > 1 and is_flag_set('endpoint.broker.joined'):
      kafka_peers = endpoint_from_flag('endpoint.broker.joined')
      broker_count = kafka_peers.kafka_broker_count()
    
    if broker_count != min_brokers:
          status.blocked("Waiting for {} units to start bootstrapping."
                        .format(min_brokers))
          return

    # Install Java
    status.maintenance('Installing Java')
    install_java()

    # Unpack Kafka files and setup user/group
    status.maintenance('Unpacking Kafka files')
    filename = resource_get('apache-kafka')
    filepath = filename and Path(filename)
    if filepath and filepath.exists() and filepath.stat().st_size:
        tar = tarfile.open(filepath, "r:gz")
        tar.extractall("/usr/lib")
        tar.close()

    distconfig = utils.DistConfig("{}/files/setup.yaml".format(charm_dir()))
    distconfig.add_users()
    distconfig.add_dirs()

    if not os.path.exists('/usr/lib/kafka'):
        # Assumes that there is only 1 kafka_* dir
        kafka_dir = glob.glob('/usr/lib/kafka_*')[0]
        os.symlink(kafka_dir, '/usr/lib/kafka')

    if not os.path.exists('/usr/lib/kafka/logs'):
        os.makedirs('/usr/lib/kafka/logs')
        os.symlink('/usr/lib/kafka/logs', '/var/log/kafka')
        os.chmod('/var/log/kafka', 0o775)
        shutil.chown('/var/log/kafka', user='kafka', group='kafka')

    # Create server.properties
    status.maintenance('Creating Kafka config')
    zookeepers = endpoint_from_flag('zookeeper.ready')
    zoo_brokers = []
    for zoo in zookeepers.zookeepers():
          zoo_brokers.append("{}:{}".format(zoo['host'], zoo['port']))

    render(source="server.properties.j2",
           target='/usr/lib/kafka/config/server.properties',
           context={
               'broker_count': min_brokers,
               'transaction_min_isr': 1 if min_brokers == 1 else min_brokers-1,
               'zookeeper_brokers': ",".join(zoo_brokers),               
           })

    # Create systemd service
    render(source='kafka.service.j2',
           target='/etc/systemd/system/kafka.service',
           context={
                 'java_home': java_home(),
                 'jmx': 1 if config().get('enable-jmx') else 0,
           })

    # Start systemd service
    status.maintenance('Starting Kafka services')
    try:
        check_call(['systemctl', 'daemon-reload'])
        check_call(['systemctl', 'start', 'kafka.service'])
        check_call(['systemctl', 'enable', 'kafka.service'])
    except CalledProcessError as e:
        log(e)        
        status.blocked('Could not start Kafka services')
        return

    open_port(9092)
    if config().get('enable-jmx'):
        open_port(9999)
    status.active('Ready')
    set_flag('kafka.installed')


@when('client.joined',
      'zookeeper.ready')
def serve_client():
    client = endpoint_from_flag('client.joined')
    zookeeper = endpoint_from_flag('zookeeper.ready')
    client.send_port(9092)
    client.send_zookeepers(zookeeper.zookeepers())


def install_java():
    java_package = "openjdk-8-jdk-headless"
    fetch.apt_install(java_package)
    java_home_ = java_home()
    utils.re_edit_in_place('/etc/environment', {
      r'#? *JAVA_HOME *=.*': 'JAVA_HOME={}'.format(java_home_),
    }, append_non_matches=True)


def java_home():
    if os.path.exists('/etc/alternatives/java'):
        return os.path.realpath('/etc/alternatives/java')[:-9]
