import re
import json

from .module import Module


class Node(Module):
    dependencies = ['cluster']
    ami_map = {
      # 'ap-southeast-2': 'ami-862211e5',  # docker enabled amazon
        'ap-southeast-2': 'ami-e7878484',  # arch linux
    }

    def __init__(self, **kwargs):
        super().__init__('node', **kwargs)

    def add_arguments(self, parser):
        subp = parser.add_subparsers(help='node help')

        p = subp.add_parser('ls', help='list nodes')
        p.add_argument('name', metavar='NAME', nargs='?', help='node name')
        p.set_defaults(node_handler=self.handle_list)

        p = subp.add_parser('mk', help='make a node')
        p.add_argument('name', metavar='NAME', help='node name')
        p.add_argument('--manager', '-m', action='store_true', help='manager node')
        p.set_defaults(node_handler=self.handle_make)

        p = subp.add_parser('ssh', help='SSH to a node')
        p.add_argument('name', metavar='NAME', nargs='?', help='node name')
        p.set_defaults(node_handler=self.handle_ssh)

        p = subp.add_parser('init', help='initialise swarm')
        p.add_argument('name', metavar='NAME', help='node name')
        p.set_defaults(node_handler=self.handle_init_swarm)

        p = subp.add_parser('join', help='join swarm')
        p.add_argument('name', metavar='NAME', help='node name')
        p.set_defaults(node_handler=self.handle_join_swarm)

        p = subp.add_parser('wait', help='wait for OK status')
        p.add_argument('name', metavar='NAME', help='node name')
        p.set_defaults(node_handler=self.handle_wait)

        p = subp.add_parser('reboot', help='reboot a node')
        p.add_argument('name', metavar='NAME', help='node name')
        p.set_defaults(node_handler=self.handle_reboot)

    def handle_list(self, args):
        self.list(args.name)

    def list(self, name):
        for name in self.get_instance_names():
            print(name)

    def handle_make(self, args):
        self.make(args.name, args.manager)

    def make(self, name, manager=False):
        self.validate(name)
        existing = self.get_instance_names()
        if name in existing:
            self.error(f'node "{name}" already exists')
        if not len(existing) and not manager:
            self.error(f'must have at least one manager before workers')
        ctx = self.get_context({'node': name})
        ec2 = self.get_boto_client('ec2')
        image = self.ami_map[ctx['region']]
        sg_id = self.client.get_module('cluster').get_security_group_id()
        with self.template_file('arch-user-data.sh', ctx) as user_data:
            inst = ec2.run_instances(
                ImageId=image,
                KeyName=f'fuku-{ctx["cluster"]}',
                SecurityGroupIds=[sg_id],
                UserData=user_data,
                InstanceType='t2.micro',
                IamInstanceProfile={
                    'Name': 'ec2-profile'
                },
                MinCount=1,
                MaxCount=1
            )
        inst_id = inst['Instances'][0]['InstanceId']
        self.tag_instance(inst_id, name, manager, ec2=ec2, ctx=ctx)
        self.wait(name)
        if manager:
            self.init_swarm(name)

    def handle_init_swarm(self, args):
        self.init_swarm(args.name)

    def init_swarm(self, name):
        ctx = self.get_context()
        inst = self.get_instance(name)
        ip = inst.private_ip_address
        resp = self.ssh_run(
            f'docker swarm init --advertise-addr {ip}',
            name,
            capture='text'
        )
        try:
            token = re.search(r'token\s+(.*)\s+\\', resp).group(1)
            port = re.search(r':(\d\d\d\d)', resp).group(1)
        except AttributeError:
            self.error('swarm initialisation failed')
        # s3 = self.get_boto_client('s3')
        # try:
        #     data = s3.get_object(
        #         Bucket=ctx['bucket'],
        #         Key=f'fuku/{ctx["cluster"]}/swarm.json',
        #     )['Body'].read().decode()
        #     data = json.loads(data)
        # except:
        #     data = {
        #         'managers': []
        #     }
        # data['managers'].append({
        #     'workertoken': token,
        #     'ip': ip,
        #     'port': port
        # })
        # s3.put_object(
        #     Bucket=ctx['bucket'],
        #     Key=f'fuku/{ctx["cluster"]}/swarm.json',
        #     Body=json.dumps(data)
        # )
        ec2 = self.get_boto_client('ec2')
        ec2.create_tags(
            Resources=[inst.id],
            Tags=[
                {'Key': 'swarmtoken', 'Value': token},
                {'Key': 'swarmport', 'Value': port}
            ]
        )
        self.ssh_run(
            'docker network create --driver overlay all',
            name=name,
            capture='discard'
        )

    def handle_join_swarm(self, args):
        self.join_swarm(args.name)

    def join_swarm(self, name):
        for inst in self.iter_managers():
            try:
                token, port = None, None
                for tag in inst.tags:
                    if tag['Key'] == 'swarmtoken':
                        token = tag['Value']
                    if tag['Key'] == 'swarmport':
                        port = tag['Value']
                if token is None:
                    raise Exception
                resp = self.ssh_run(
                    f'docker swarm join --token {token} {inst.private_ip_address}:{port}',
                    name=name,
                    capture='text'
                )
                if resp != 'This node joined a swarm as a worker.':
                    self.error('failed to join swarm')
            except:
                pass
            else:
                break

    def handle_wait(self, args):
        self.wait(args.name)

    def wait(self, name):
        inst = self.get_instance(name)
        ec2 = self.get_boto_client('ec2')
        waiter = ec2.get_waiter('instance_status_ok')
        waiter.wait(InstanceIds=[inst.id])

    def handle_ssh(self, args):
        self.ssh_run('', args.name)

    def handle_reboot(self, args):
        self.reboot(args.name)

    def reboot(self, name):
        ctx = self.get_context()
        try:
            name = name or ctx['node']
        except KeyError:
            self.error('unknown node')
        inst = self.get_instance(name)
        inst.reboot()

    def tag_instance(self, inst_id, name, manager=False, ec2=None, ctx=None):
        if ctx is None:
            ctx = self.get_context()
        if ec2 is None:
            ec2 = self.get_boto_resource('ec2')
        ec2.create_tags(
            Resources=[inst_id],
            Tags=[
                {'Key': 'Name', 'Value': f'{ctx["cluster"]}-{name}'},
                {'Key': 'name', 'Value': name},
                {'Key': 'cluster', 'Value': ctx['cluster']},
                {'Key': 'node', 'Value': 'manager' if manager else 'worker'}
            ]
        )

    def iter_instances(self):
        ec2 = self.get_boto_resource('ec2')
        cluster = self.client.get_selected('cluster')
        filters = [
            {
                'Name': 'tag:cluster',
                'Values': [cluster]
            },
            {
                'Name': 'instance-state-name',
                'Values': ['pending', 'running', 'shutting-down', 'stopping', 'stopped']
            }
        ]
        for inst in ec2.instances.filter(Filters=filters):
            yield inst

    def get_instance_names(self):
        names = []
        for inst in self.iter_instances():
            for tag in inst.tags:
                if tag['Key'] == 'name':
                    names.append(tag['Value'])
                    break
        return names

    def get_instance(self, name):
        ec2 = self.get_boto_resource('ec2')
        cluster = self.client.get_selected('cluster')
        filters = [
            {
                'Name': 'tag:cluster',
                'Values': [cluster]
            },
            {
                'Name': 'tag:name',
                'Values': [name]
            },
            {
                'Name': 'instance-state-name',
                'Values': ['pending', 'running', 'shutting-down', 'stopping', 'stopped']
            }
        ]
        insts = list(ec2.instances.filter(Filters=filters))
        if not len(insts):
            self.error(f'no node in cluster "{cluster}" with name "{name}"')
        return insts[0]

    def iter_managers(self):
        ctx = self.get_context()
        ec2 = self.get_boto_resource('ec2')
        filters = [
            {
                'Name': 'tag:cluster',
                'Values': [ctx['cluster']]
            },
            {
                'Name': 'tag:node',
                'Values': ['manager']
            },
            {
                'Name': 'instance-state-name',
                'Values': ['running']
            }
        ]
        for inst in ec2.instances.filter(Filters=filters):
            yield inst

    def ssh_run(self, cmd, name=None, inst=None, tty=False, capture=None):
        ctx = self.get_context()
        if inst is None:
            name = name or ctx['node']
            inst = self.get_instance(name)
        ip = inst.public_ip_address
        full_cmd = f'ssh{" -t" if tty else ""} -o "StrictHostKeyChecking no" -i "{ctx["pem"]}" root@{ip} {cmd}'
        return self.run(full_cmd, capture=capture)

    def mgr_run(self, cmd, tty=False, capture=None):
        try:
            mgr = list(self.iter_managers())[0]
        except IndexError:
            self.error('no managers available')
        return self.ssh_run(cmd, inst=mgr, tty=tty, capture=capture)

    def get_my_context(self):
        ctx = {}
        node = self.store_get('selected')
        if node is not None:
            ctx['node'] = node
        return ctx