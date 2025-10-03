import discord
from discord.ext import commands
from discord import ui, app_commands
import os
import random
import string
import json
import subprocess
from dotenv import load_dotenv
import asyncio
import datetime
import docker
import time
import logging
import traceback
import aiohttp
import socket
import re
import psutil
import platform
import shutil
from typing import Optional, Literal
import sqlite3
import pickle
import base64
import threading
from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit
import docker
import paramiko
import os
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('cloudnode_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('CloudNodeBot')

# Load environment variables
load_dotenv()

# Bot configuration
TOKEN = os.getenv('DISCORD_TOKEN')
ADMIN_IDS = {int(id_) for id_ in os.getenv('ADMIN_IDS', '1210291131301101618').split(',') if id_.strip()}
ADMIN_ROLE_ID = int(os.getenv('ADMIN_ROLE_ID', '1376177459870961694'))
WATERMARK = "CloudNode VPS Service"
WELCOME_MESSAGE = "Welcome To CloudNode! Get Started With Us!"
MAX_VPS_PER_USER = int(os.getenv('MAX_VPS_PER_USER', '3'))
DEFAULT_OS_IMAGE = os.getenv('DEFAULT_OS_IMAGE', 'ubuntu:22.04')
DOCKER_NETWORK = os.getenv('DOCKER_NETWORK', 'bridge')
MAX_CONTAINERS = int(os.getenv('MAX_CONTAINERS', '100'))
DB_FILE = 'cloudnode.db'
BACKUP_FILE = 'cloudnode_backup.pkl'

# Known miner process names/patterns
MINER_PATTERNS = [
    'xmrig', 'ethminer', 'cgminer', 'sgminer', 'bfgminer',
    'minerd', 'cpuminer', 'cryptonight', 'stratum', 'pool'
]

# Dockerfile template for custom images
DOCKERFILE_TEMPLATE = """
FROM {base_image}

# Prevent prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install systemd, sudo, SSH, Docker and other essential packages
RUN apt-get update && \\
    apt-get install -y systemd systemd-sysv dbus sudo \\
                       curl gnupg2 apt-transport-https ca-certificates \\
                       software-properties-common \\
                       docker.io openssh-server tmate && \\
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Root password
RUN echo "root:{root_password}" | chpasswd

# Create user and set password
RUN useradd -m -s /bin/bash {username} && \\
    echo "{username}:{user_password}" | chpasswd && \\
    usermod -aG sudo {username}

# Enable SSH login
RUN mkdir /var/run/sshd && \\
    sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config && \\
    sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config

# Enable services on boot
RUN systemctl enable ssh && \\
    systemctl enable docker

# CloudNode customization
RUN echo '{welcome_message}' > /etc/motd && \\
    echo 'echo "{welcome_message}"' >> /home/{username}/.bashrc && \\
    echo '{watermark}' > /etc/machine-info && \\
    echo 'cloudnode-{vps_id}' > /etc/hostname

# Install additional useful packages
RUN apt-get update && \\
    apt-get install -y neofetch htop nano vim wget git tmux net-tools dnsutils iputils-ping && \\
    apt-get clean && \\
    rm -rf /var/lib/apt/lists/*

# Fix systemd inside container
STOPSIGNAL SIGRTMIN+3

# Boot into systemd (like a VM)
CMD ["/sbin/init"]
"""

class Database:
    """Handles all data persistence using SQLite3"""
    def __init__(self, db_file):
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_tables()
        self._initialize_settings()

    def _create_tables(self):
        """Create necessary tables"""
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS vps_instances (
                token TEXT PRIMARY KEY,
                vps_id TEXT UNIQUE,
                container_id TEXT,
                memory INTEGER,
                cpu INTEGER,
                disk INTEGER,
                username TEXT,
                password TEXT,
                root_password TEXT,
                created_by TEXT,
                created_at TEXT,
                tmate_session TEXT,
                watermark TEXT,
                os_image TEXT,
                restart_count INTEGER DEFAULT 0,
                last_restart TEXT,
                status TEXT DEFAULT 'running',
                use_custom_image BOOLEAN DEFAULT 1
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS usage_stats (
                key TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id TEXT PRIMARY KEY
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_users (
                user_id TEXT PRIMARY KEY
            )
        ''')
        
        self.conn.commit()

    def _initialize_settings(self):
        """Initialize default settings"""
        defaults = {
            'max_containers': str(MAX_CONTAINERS),
            'max_vps_per_user': str(MAX_VPS_PER_USER)
        }
        for key, value in defaults.items():
            self.cursor.execute('INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)', (key, value))
        
        # Load admin users from database
        self.cursor.execute('SELECT user_id FROM admin_users')
        for row in self.cursor.fetchall():
            ADMIN_IDS.add(int(row[0]))
            
        self.conn.commit()

    def get_setting(self, key, default=None):
        self.cursor.execute('SELECT value FROM system_settings WHERE key = ?', (key,))
        result = self.cursor.fetchone()
        return int(result[0]) if result else default

    def set_setting(self, key, value):
        self.cursor.execute('INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)', (key, str(value)))
        self.conn.commit()

    def get_stat(self, key, default=0):
        self.cursor.execute('SELECT value FROM usage_stats WHERE key = ?', (key,))
        result = self.cursor.fetchone()
        return result[0] if result else default

    def increment_stat(self, key, amount=1):
        current = self.get_stat(key)
        self.cursor.execute('INSERT OR REPLACE INTO usage_stats (key, value) VALUES (?, ?)', (key, current + amount))
        self.conn.commit()

    def get_vps_by_id(self, vps_id):
        self.cursor.execute('SELECT * FROM vps_instances WHERE vps_id = ?', (vps_id,))
        row = self.cursor.fetchone()
        if not row:
            return None, None
        columns = [desc[0] for desc in self.cursor.description]
        vps = dict(zip(columns, row))
        return vps['token'], vps

    def get_vps_by_token(self, token):
        self.cursor.execute('SELECT * FROM vps_instances WHERE token = ?', (token,))
        row = self.cursor.fetchone()
        if not row:
            return None
        columns = [desc[0] for desc in self.cursor.description]
        return dict(zip(columns, row))

    def get_user_vps_count(self, user_id):
        self.cursor.execute('SELECT COUNT(*) FROM vps_instances WHERE created_by = ?', (str(user_id),))
        return self.cursor.fetchone()[0]

    def get_user_vps(self, user_id):
        self.cursor.execute('SELECT * FROM vps_instances WHERE created_by = ?', (str(user_id),))
        columns = [desc[0] for desc in self.cursor.description]
        return [dict(zip(columns, row)) for row in self.cursor.fetchall()]

    def get_all_vps(self):
        self.cursor.execute('SELECT * FROM vps_instances')
        columns = [desc[0] for desc in self.cursor.description]
        return {row[0]: dict(zip(columns, row)) for row in self.cursor.fetchall()}

    def add_vps(self, vps_data):
        columns = ', '.join(vps_data.keys())
        placeholders = ', '.join('?' for _ in vps_data)
        self.cursor.execute(f'INSERT INTO vps_instances ({columns}) VALUES ({placeholders})', tuple(vps_data.values()))
        self.conn.commit()
        self.increment_stat('total_vps_created')

    def remove_vps(self, token):
        self.cursor.execute('DELETE FROM vps_instances WHERE token = ?', (token,))
        self.conn.commit()
        return self.cursor.rowcount > 0

    def update_vps(self, token, updates):
        set_clause = ', '.join(f'{k} = ?' for k in updates)
        values = list(updates.values()) + [token]
        self.cursor.execute(f'UPDATE vps_instances SET {set_clause} WHERE token = ?', values)
        self.conn.commit()
        return self.cursor.rowcount > 0

    def is_user_banned(self, user_id):
        self.cursor.execute('SELECT 1 FROM banned_users WHERE user_id = ?', (str(user_id),))
        return self.cursor.fetchone() is not None

    def ban_user(self, user_id):
        self.cursor.execute('INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)', (str(user_id),))
        self.conn.commit()

    def unban_user(self, user_id):
        self.cursor.execute('DELETE FROM banned_users WHERE user_id = ?', (str(user_id),))
        self.conn.commit()

    def get_banned_users(self):
        self.cursor.execute('SELECT user_id FROM banned_users')
        return [row[0] for row in self.cursor.fetchall()]

    def add_admin(self, user_id):
        self.cursor.execute('INSERT OR IGNORE INTO admin_users (user_id) VALUES (?)', (str(user_id),))
        self.conn.commit()
        ADMIN_IDS.add(int(user_id))

    def remove_admin(self, user_id):
        self.cursor.execute('DELETE FROM admin_users WHERE user_id = ?', (str(user_id),))
        self.conn.commit()
        if int(user_id) in ADMIN_IDS:
            ADMIN_IDS.remove(int(user_id))

    def get_admins(self):
        self.cursor.execute('SELECT user_id FROM admin_users')
        return [row[0] for row in self.cursor.fetchall()]

    def backup_data(self):
        """Backup all data to a file"""
        data = {
            'vps_instances': self.get_all_vps(),
            'usage_stats': {},
            'system_settings': {},
            'banned_users': self.get_banned_users(),
            'admin_users': self.get_admins()
        }
        
        # Get usage stats
        self.cursor.execute('SELECT * FROM usage_stats')
        for row in self.cursor.fetchall():
            data['usage_stats'][row[0]] = row[1]
            
        # Get system settings
        self.cursor.execute('SELECT * FROM system_settings')
        for row in self.cursor.fetchall():
            data['system_settings'][row[0]] = row[1]
            
        with open(BACKUP_FILE, 'wb') as f:
            pickle.dump(data, f)
            
        return True

    def restore_data(self):
        """Restore data from backup file"""
        if not os.path.exists(BACKUP_FILE):
            return False
            
        try:
            with open(BACKUP_FILE, 'rb') as f:
                data = pickle.load(f)
                
            # Clear all tables
            self.cursor.execute('DELETE FROM vps_instances')
            self.cursor.execute('DELETE FROM usage_stats')
            self.cursor.execute('DELETE FROM system_settings')
            self.cursor.execute('DELETE FROM banned_users')
            self.cursor.execute('DELETE FROM admin_users')
            
            # Restore VPS instances
            for token, vps in data['vps_instances'].items():
                columns = ', '.join(vps.keys())
                placeholders = ', '.join('?' for _ in vps)
                self.cursor.execute(f'INSERT INTO vps_instances ({columns}) VALUES ({placeholders})', tuple(vps.values()))
            
            # Restore usage stats
            for key, value in data['usage_stats'].items():
                self.cursor.execute('INSERT INTO usage_stats (key, value) VALUES (?, ?)', (key, value))
                
            # Restore system settings
            for key, value in data['system_settings'].items():
                self.cursor.execute('INSERT INTO system_settings (key, value) VALUES (?, ?)', (key, value))
                
            # Restore banned users
            for user_id in data['banned_users']:
                self.cursor.execute('INSERT INTO banned_users (user_id) VALUES (?)', (user_id,))
                
            # Restore admin users
            for user_id in data['admin_users']:
                self.cursor.execute('INSERT INTO admin_users (user_id) VALUES (?)', (user_id,))
                ADMIN_IDS.add(int(user_id))
                
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error restoring data: {e}")
            return False

    def close(self):
        self.conn.close()

# Initialize bot with command prefix '/'
class CloudNodeBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db = Database(DB_FILE)
        self.session = None
        self.docker_client = None
        self.system_stats = {
            'cpu_usage': 0,
            'memory_usage': 0,
            'disk_usage': 0,
            'network_io': (0, 0),
            'last_updated': 0
        }
        self.my_persistent_views = {}

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        try:
            self.docker_client = docker.from_env()
            logger.info("Docker client initialized successfully")
            self.loop.create_task(self.update_system_stats())
            self.loop.create_task(self.anti_miner_monitor())
            # Reconnect to existing containers
            await self.reconnect_containers()
            # Restore persistent views
            await self.restore_persistent_views()
        except Exception as e:
            logger.error(f"Failed to initialize Docker client: {e}")
            self.docker_client = None

    async def reconnect_containers(self):
        """Reconnect to existing containers on startup"""
        if not self.docker_client:
            return
            
        for token, vps in list(self.db.get_all_vps().items()):
            if vps['status'] == 'running':
                try:
                    container = self.docker_client.containers.get(vps['container_id'])
                    if container.status != 'running':
                        container.start()
                    logger.info(f"Reconnected and started container for VPS {vps['vps_id']}")
                except docker.errors.NotFound:
                    logger.warning(f"Container {vps['container_id']} not found, removing from data")
                    self.db.remove_vps(token)
                except Exception as e:
                    logger.error(f"Error reconnecting container {vps['vps_id']}: {e}")

    async def restore_persistent_views(self):
        """Restore persistent views after restart"""
        # This would be implemented to restore any persistent UI components
        pass

    async def anti_miner_monitor(self):
        """Periodically check for mining activities"""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                for token, vps in self.db.get_all_vps().items():
                    if vps['status'] != 'running':
                        continue
                    try:
                        container = self.docker_client.containers.get(vps['container_id'])
                        if container.status != 'running':
                            continue
                        
                        # Check processes
                        exec_result = container.exec_run("ps aux")
                        output = exec_result.output.decode().lower()
                        
                        for pattern in MINER_PATTERNS:
                            if pattern in output:
                                logger.warning(f"Mining detected in VPS {vps['vps_id']}, suspending...")
                                container.stop()
                                self.db.update_vps(token, {'status': 'suspended'})
                                # Notify owner
                                try:
                                    owner = await self.fetch_user(int(vps['created_by']))
                                    await owner.send(f"⚠️ Your VPS {vps['vps_id']} has been suspended due to detected mining activity. Contact admin to unsuspend.")
                                except:
                                    pass
                                break
                    except Exception as e:
                        logger.error(f"Error checking VPS {vps['vps_id']} for mining: {e}")
            except Exception as e:
                logger.error(f"Error in anti_miner_monitor: {e}")
            await asyncio.sleep(300)  # Check every 5 minutes

    async def update_system_stats(self):
        """Update system statistics periodically"""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                # CPU usage
                cpu_percent = psutil.cpu_percent(interval=1)
                
                # Memory usage
                mem = psutil.virtual_memory()
                
                # Disk usage
                disk = psutil.disk_usage('/')
                
                # Network IO
                net_io = psutil.net_io_counters()
                
                self.system_stats = {
                    'cpu_usage': cpu_percent,
                    'memory_usage': mem.percent,
                    'memory_used': mem.used / (1024 ** 3),  # GB
                    'memory_total': mem.total / (1024 ** 3),  # GB
                    'disk_usage': disk.percent,
                    'disk_used': disk.used / (1024 ** 3),  # GB
                    'disk_total': disk.total / (1024 ** 3),  # GB
                    'network_sent': net_io.bytes_sent / (1024 ** 2),  # MB
                    'network_recv': net_io.bytes_recv / (1024 ** 2),  # MB
                    'last_updated': time.time()
                }
            except Exception as e:
                logger.error(f"Error updating system stats: {e}")
            await asyncio.sleep(30)

    async def close(self):
        await super().close()
        if self.session:
            await self.session.close()
        if self.docker_client:
            self.docker_client.close()
        self.db.close()

def generate_token():
    """Generate a random token for VPS access"""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=24))

def generate_vps_id():
    """Generate a unique VPS ID"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))

def generate_ssh_password():
    """Generate a random SSH password"""
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(random.choices(chars, k=16))

def has_admin_role(ctx):
    """Check if user has admin role or is in ADMIN_IDS"""
    if isinstance(ctx, discord.Interaction):
        user_id = ctx.user.id
        roles = ctx.user.roles
    else:
        user_id = ctx.author.id
        roles = ctx.author.roles
    
    if user_id in ADMIN_IDS:
        return True
    
    return any(role.id == ADMIN_ROLE_ID for role in roles)

async def capture_ssh_session_line(process):
    """Capture the SSH session line from tmate output"""
    try:
        while True:
            output = await process.stdout.readline()
            if not output:
                break
            output = output.decode('utf-8').strip()
            if "ssh session:" in output:
                return output.split("ssh session:")[1].strip()
        return None
    except Exception as e:
        logger.error(f"Error capturing SSH session: {e}")
        return None

async def run_docker_command(container_id, command, timeout=120):
    """Run a Docker command asynchronously with timeout"""
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            if process.returncode != 0:
                raise Exception(f"Command failed: {stderr.decode()}")
            return True, stdout.decode()
        except asyncio.TimeoutError:
            process.kill()
            raise Exception(f"Command timed out after {timeout} seconds")
    except Exception as e:
        logger.error(f"Error running Docker command: {e}")
        return False, str(e)

async def kill_apt_processes(container_id):
    """Kill any running apt processes"""
    try:
        success, _ = await run_docker_command(container_id, ["bash", "-c", "killall apt apt-get dpkg || true"])
        await asyncio.sleep(2)
        success, _ = await run_docker_command(container_id, ["bash", "-c", "rm -f /var/lib/apt/lists/lock /var/cache/apt/archives/lock /var/lib/dpkg/lock*"])
        await asyncio.sleep(2)
        return success
    except Exception as e:
        logger.error(f"Error killing apt processes: {e}")
        return False

async def wait_for_apt_lock(container_id, status_msg):
    """Wait for apt lock to be released"""
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            await kill_apt_processes(container_id)
            
            process = await asyncio.create_subprocess_exec(
                "docker", "exec", container_id, "bash", "-c", "lsof /var/lib/dpkg/lock-frontend",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                return True
                
            if isinstance(status_msg, discord.Interaction):
                await status_msg.followup.send(f"🔄 Waiting for package manager to be ready... (Attempt {attempt + 1}/{max_attempts})", ephemeral=True)
            else:
                await status_msg.edit(content=f"🔄 Waiting for package manager to be ready... (Attempt {attempt + 1}/{max_attempts})")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Error checking apt lock: {e}")
            await asyncio.sleep(5)
    
    return False

async def build_custom_image(vps_id, username, root_password, user_password, base_image=DEFAULT_OS_IMAGE):
    """Build a custom Docker image using our template"""
    try:
        # Create a temporary directory for the Dockerfile
        temp_dir = f"temp_dockerfiles/{vps_id}"
        os.makedirs(temp_dir, exist_ok=True)
        
        # Generate Dockerfile content
        dockerfile_content = DOCKERFILE_TEMPLATE.format(
            base_image=base_image,
            root_password=root_password,
            username=username,
            user_password=user_password,
            welcome_message=WELCOME_MESSAGE,
            watermark=WATERMARK,
            vps_id=vps_id
        )
        
        # Write Dockerfile
        dockerfile_path = os.path.join(temp_dir, "Dockerfile")
        with open(dockerfile_path, 'w') as f:
            f.write(dockerfile_content)
        
        # Build the image
        image_tag = f"cloudnode/{vps_id.lower()}:latest"
        build_process = await asyncio.create_subprocess_exec(
            "docker", "build", "-t", image_tag, temp_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await build_process.communicate()
        
        if build_process.returncode != 0:
            raise Exception(f"Failed to build image: {stderr.decode()}")
        
        return image_tag
    except Exception as e:
        logger.error(f"Error building custom image: {e}")
        raise
    finally:
        # Clean up temporary directory
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
        except Exception as e:
            logger.error(f"Error cleaning up temp directory: {e}")

async def setup_container(container_id, status_msg, memory, username, vps_id=None, use_custom_image=False):
    """Enhanced container setup with CloudNode customization"""
    try:
        # Ensure container is running
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("🔍 Checking container status...", ephemeral=True)
        else:
            await status_msg.edit(content="🔍 Checking container status...")
            
        container = bot.docker_client.containers.get(container_id)
        if container.status != "running":
            if isinstance(status_msg, discord.Interaction):
                await status_msg.followup.send("🚀 Starting container...", ephemeral=True)
            else:
                await status_msg.edit(content="🚀 Starting container...")
            container.start()
            await asyncio.sleep(5)

        # Generate SSH password
        ssh_password = generate_ssh_password()
        
        # Install tmate and other required packages
        if not use_custom_image:
            if isinstance(status_msg, discord.Interaction):
                await status_msg.followup.send("📦 Installing required packages...", ephemeral=True)
            else:
                await status_msg.edit(content="📦 Installing required packages...")
                
            # Update package list
            success, output = await run_docker_command(container_id, ["apt-get", "update"])
            if not success:
                raise Exception(f"Failed to update package list: {output}")

            # Install packages
            packages = [
                "tmate", "neofetch", "screen", "wget", "curl", "htop", "nano", "vim", 
                "openssh-server", "sudo", "ufw", "git", "docker.io", "systemd", "systemd-sysv"
            ]
            success, output = await run_docker_command(container_id, ["apt-get", "install", "-y"] + packages)
            if not success:
                raise Exception(f"Failed to install packages: {output}")

        # Setup SSH
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("🔐 Configuring SSH access...", ephemeral=True)
        else:
            await status_msg.edit(content="🔐 Configuring SSH access...")
            
        # Create user and set password (if not using custom image)
        if not use_custom_image:
            user_setup_commands = [
                f"useradd -m -s /bin/bash {username}",
                f"echo '{username}:{ssh_password}' | chpasswd",
                f"usermod -aG sudo {username}",
                "sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin no/' /etc/ssh/sshd_config",
                "sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config",
                "service ssh restart"
            ]
            
            for cmd in user_setup_commands:
                success, output = await run_docker_command(container_id, ["bash", "-c", cmd])
                if not success:
                    raise Exception(f"Failed to setup user: {output}")

        # Set CloudNode customization
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("🎨 Setting up CloudNode customization...", ephemeral=True)
        else:
            await status_msg.edit(content="🎨 Setting up CloudNode customization...")
            
        # Create welcome message file
        welcome_cmd = f"echo '{WELCOME_MESSAGE}' > /etc/motd && echo 'echo \"{WELCOME_MESSAGE}\"' >> /home/{username}/.bashrc"
        success, output = await run_docker_command(container_id, ["bash", "-c", welcome_cmd])
        if not success:
            logger.warning(f"Could not set welcome message: {output}")

        # Set hostname and watermark
        if not vps_id:
            vps_id = generate_vps_id()
        hostname_cmd = f"echo 'cloudnode-{vps_id}' > /etc/hostname && hostname cloudnode-{vps_id}"
        success, output = await run_docker_command(container_id, ["bash", "-c", hostname_cmd])
        if not success:
            raise Exception(f"Failed to set hostname: {output}")

        # Set memory limit in cgroup
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("⚙️ Setting resource limits...", ephemeral=True)
        else:
            await status_msg.edit(content="⚙️ Setting resource limits...")
            
        memory_bytes = memory * 1024 * 1024 * 1024
        success, output = await run_docker_command(container_id, ["bash", "-c", f"echo {memory_bytes} > /sys/fs/cgroup/memory.max"])
        if not success:
            logger.warning(f"Could not set memory limit in cgroup: {output}")

        # Set watermark in machine info
        success, output = await run_docker_command(container_id, ["bash", "-c", f"echo '{WATERMARK}' > /etc/machine-info"])
        if not success:
            logger.warning(f"Could not set machine info: {output}")

        # Basic security setup
        security_commands = [
            "ufw allow ssh",
            "ufw --force enable",
            "apt-get -y autoremove",
            "apt-get clean",
            f"chown -R {username}:{username} /home/{username}",
            f"chmod 700 /home/{username}"
        ]
        
        for cmd in security_commands:
            success, output = await run_docker_command(container_id, ["bash", "-c", cmd])
            if not success:
                logger.warning(f"Security setup command failed: {cmd} - {output}")

        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("✅ CloudNode VPS setup completed successfully!", ephemeral=True)
        else:
            await status_msg.edit(content="✅ CloudNode VPS setup completed successfully!")
            
        return True, ssh_password, vps_id
    except Exception as e:
        error_msg = f"Setup failed: {str(e)}"
        logger.error(error_msg)
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send(f"❌ {error_msg}", ephemeral=True)
        else:
            await status_msg.edit(content=f"❌ {error_msg}")
        return False, None, None

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = CloudNodeBot(command_prefix='/', intents=intents, help_command=None)

@bot.event
async def on_ready():
    logger.info(f'{bot.user} has connected to Discord!')
    
    # Auto-start VPS containers based on status
    if bot.docker_client:
        for token, vps in bot.db.get_all_vps().items():
            if vps['status'] == 'running':
                try:
                    container = bot.docker_client.containers.get(vps["container_id"])
                    if container.status != "running":
                        container.start()
                        logger.info(f"Started container for VPS {vps['vps_id']}")
                except docker.errors.NotFound:
                    logger.warning(f"Container {vps['container_id']} not found")
                except Exception as e:
                    logger.error(f"Error starting container: {e}")
    
    try:
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="CloudNode VPS"))
        synced_commands = await bot.tree.sync()
        logger.info(f"Synced {len(synced_commands)} slash commands")
    except Exception as e:
        logger.error(f"Error syncing slash commands: {e}")

@bot.hybrid_command(name='help', description='Show all available commands')
async def show_commands(ctx):
    """Show all available commands"""
    try:
        embed = discord.Embed(title="🤖 CloudNode VPS Bot Commands", color=discord.Color.blue())
        
        # User commands
        embed.add_field(name="User Commands", value="""
`/create_vps` - Create a new VPS (Admin only)
`/connect_vps <token>` - Connect to your VPS
`/list` - List all your VPS instances
`/help` - Show this help message
`/manage_vps <vps_id>` - Manage your VPS
`/transfer_vps <vps_id> <user>` - Transfer VPS ownership
`/vps_stats <vps_id>` - Show VPS resource usage
`/change_ssh_password <vps_id>` - Change SSH password
`/vps_shell <vps_id>` - Get shell access to your VPS
`/vps_console <vps_id>` - Get direct console access to your VPS
`/vps_usage` - Show your VPS usage statistics
""", inline=False)
        
        # Admin commands
        if has_admin_role(ctx):
            embed.add_field(name="Admin Commands", value="""
`/vps_list` - List all VPS instances
`/delete_vps <vps_id>` - Delete a VPS
`/admin_stats` - Show system statistics
`/cleanup_vps` - Cleanup inactive VPS instances
`/add_admin <user>` - Add a new admin
`/remove_admin <user>` - Remove an admin (Owner only)
`/list_admins` - List all admin users
`/system_info` - Show detailed system information
`/container_limit <max>` - Set maximum container limit
`/global_stats` - Show global usage statistics
`/migrate_vps <vps_id>` - Migrate VPS to another host
`/emergency_stop <vps_id>` - Force stop a problematic VPS
`/emergency_remove <vps_id>` - Force remove a problematic VPS
`/suspend_vps <vps_id>` - Suspend a VPS
`/unsuspend_vps <vps_id>` - Unsuspend a VPS
`/edit_vps <vps_id> <memory> <cpu> <disk>` - Edit VPS specifications
`/ban_user <user>` - Ban a user from creating VPS
`/unban_user <user>` - Unban a user
`/list_banned` - List banned users
`/backup_data` - Backup all data
`/restore_data` - Restore from backup
`/reinstall_bot` - Reinstall the bot (Owner only)
""", inline=False)
        
        await ctx.send(embed=embed)
    except Exception as e:
        logger.error(f"Error in show_commands: {e}")
        await ctx.send("❌ An error occurred while processing your request.")

@bot.hybrid_command(name='add_admin', description='Add a new admin (Admin only)')
@app_commands.describe(
    user="User to make admin"
)
async def add_admin(ctx, user: discord.User):
    """Add a new admin user"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    
    bot.db.add_admin(user.id)
    await ctx.send(f"✅ {user.mention} has been added as an admin!")

@bot.hybrid_command(name='remove_admin', description='Remove an admin (Owner only)')
@app_commands.describe(
    user="User to remove from admin"
)
async def remove_admin(ctx, user: discord.User):
    """Remove an admin user"""
    if not has_admin_role(ctx) or ctx.author.id != ADMIN_IDS[0]:
        await ctx.send("❌ Only the bot owner can remove admins!", ephemeral=True)
        return
    
    bot.db.remove_admin(user.id)
    await ctx.send(f"✅ {user.mention} has been removed from admins!")

@bot.hybrid_command(name='list_admins', description='List all admin users')
async def list_admins(ctx):
    """List all admin users"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    
    admins = bot.db.get_admins()
    embed = discord.Embed(title="👑 Admin Users", color=discord.Color.gold())
    
    admin_list = []
    for admin_id in admins:
        try:
            user = await bot.fetch_user(int(admin_id))
            admin_list.append(f"{user.mention} (`{user.id}`)")
        except:
            admin_list.append(f"Unknown User (`{admin_id}`)")
    
    embed.description = "\n".join(admin_list) if admin_list else "No admin users found"
    await ctx.send(embed=embed)

@bot.hybrid_command(name='create_vps', description='Create a new VPS instance (Admin only)')
@app_commands.describe(
    memory="Memory in GB",
    cpu="CPU cores",
    disk="Disk space in GB",
    user="User who will own this VPS",
    os_image="OS image to use (default: ubuntu:22.04)",
    use_custom_image="Use CloudNode custom image (recommended)"
)
async def create_vps(ctx, memory: int, cpu: int, disk: int, user: discord.User, os_image: str = DEFAULT_OS_IMAGE, use_custom_image: bool = True):
    """Create a new VPS instance"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to create VPS instances!", ephemeral=True)
        return
    
    if bot.db.is_user_banned(user.id):
        await ctx.send("❌ This user is banned from creating VPS instances!", ephemeral=True)
        return
    
    user_vps_count = bot.db.get_user_vps_count(user.id)
    if user_vps_count >= bot.db.get_setting('max_vps_per_user', MAX_VPS_PER_USER):
        await ctx.send(f"❌ {user.mention} has reached the maximum limit of {bot.db.get_setting('max_vps_per_user', MAX_VPS_PER_USER)} VPS instances!", ephemeral=True)
        return
    
    total_containers = len(bot.db.get_all_vps())
    max_containers = bot.db.get_setting('max_containers', MAX_CONTAINERS)
    if total_containers >= max_containers:
        await ctx.send(f"❌ Maximum container limit ({max_containers}) reached! Cannot create more VPS instances.", ephemeral=True)
        return
    
    # Validate memory and CPU limits
    if memory > 32:
        await ctx.send("❌ Memory cannot exceed 32GB!", ephemeral=True)
        return
    if cpu > 16:
        await ctx.send("❌ CPU cores cannot exceed 16!", ephemeral=True)
        return
    if disk > 100:
        await ctx.send("❌ Disk space cannot exceed 100GB!", ephemeral=True)
        return
    
    status_msg = await ctx.send("🚀 Creating your CloudNode VPS...")
    
    try:
        # Generate unique IDs and passwords
        token = generate_token()
        vps_id = generate_vps_id()
        username = "cloudnode"
        root_password = generate_ssh_password()
        user_password = generate_ssh_password()
        
        await status_msg.edit(content="🔧 Building CloudNode environment...")
        
        # Build custom image if requested
        image_to_use = os_image
        if use_custom_image:
            try:
                await status_msg.edit(content="🏗️ Building custom CloudNode image...")
                image_to_use = await build_custom_image(vps_id, username, root_password, user_password, os_image)
            except Exception as e:
                logger.error(f"Custom image build failed, using base image: {e}")
                await status_msg.edit(content="⚠️ Custom image build failed, using base image...")
                use_custom_image = False
                image_to_use = os_image
        
        # Create Docker container
        await status_msg.edit(content="🐳 Creating Docker container...")
        
        container_config = {
            'image': image_to_use,
            'detach': True,
            'tty': True,
            'stdin_open': True,
            'mem_limit': f'{memory}g',
            'memswap_limit': f'{memory}g',
            'cpu_period': 100000,
            'cpu_quota': cpu * 100000,
            'network_mode': DOCKER_NETWORK,
            'hostname': f'cloudnode-{vps_id}',
            'security_opt': ['seccomp=unconfined'],
            'cap_add': ['SYS_ADMIN', 'NET_ADMIN'],
            'tmpfs': {'/run': '', '/run/lock': ''},
            'volumes': {
                '/sys/fs/cgroup': {'bind': '/sys/fs/cgroup', 'mode': 'ro'}
            }
        }
        
        container = bot.docker_client.containers.create(**container_config)
        container.start()
        
        await status_msg.edit(content="⚙️ Setting up CloudNode VPS...")
        
        # Setup container
        success, ssh_password, final_vps_id = await setup_container(
            container.id, status_msg, memory, username, vps_id, use_custom_image
        )
        
        if not success:
            raise Exception("Failed to setup container")
        
        # Get tmate session
        await status_msg.edit(content="🔗 Setting up SSH access...")
        try:
            tmate_process = await asyncio.create_subprocess_exec(
                "docker", "exec", container.id, "sudo", "-u", username, "tmate", "-S", "/tmp/tmate.sock", "new-session", "-d",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.sleep(3)
            
            tmate_web_process = await asyncio.create_subprocess_exec(
                "docker", "exec", container.id, "sudo", "-u", username, "tmate", "-S", "/tmp/tmate.sock", "wait", "tmate-ready",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await tmate_web_process.wait()
            
            tmate_show_process = await asyncio.create_subprocess_exec(
                "docker", "exec", container.id, "sudo", "-u", username, "tmate", "-S", "/tmp/tmate.sock", "display", "-p", "#{{tmate_ssh}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            tmate_output, _ = await tmate_show_process.communicate()
            tmate_session = tmate_output.decode().strip()
        except Exception as e:
            logger.error(f"Error setting up tmate: {e}")
            tmate_session = None
        
        # Store VPS data
        vps_data = {
            'token': token,
            'vps_id': final_vps_id,
            'container_id': container.id,
            'memory': memory,
            'cpu': cpu,
            'disk': disk,
            'username': username,
            'password': ssh_password,
            'root_password': root_password,
            'created_by': str(user.id),
            'created_at': datetime.datetime.now().isoformat(),
            'tmate_session': tmate_session,
            'watermark': WATERMARK,
            'os_image': os_image,
            'use_custom_image': use_custom_image
        }
        bot.db.add_vps(vps_data)
        
        # Send DM to user
        try:
            embed = discord.Embed(
                title="🚀 Your CloudNode VPS is Ready!",
                description=f"Your VPS has been successfully created with ID: `{final_vps_id}`",
                color=discord.Color.green(),
                timestamp=datetime.datetime.now()
            )
            
            embed.add_field(name="🔑 VPS ID", value=f"`{final_vps_id}`", inline=True)
            embed.add_field(name="🔐 Token", value=f"`{token}`", inline=True)
            embed.add_field(name="💾 Memory", value=f"{memory}GB", inline=True)
            embed.add_field(name="⚡ CPU Cores", value=f"{cpu}", inline=True)
            embed.add_field(name="💿 Disk Space", value=f"{disk}GB", inline=True)
            embed.add_field(name="🐧 OS", value=os_image, inline=True)
            
            if use_custom_image:
                embed.add_field(name="👤 Username", value=username, inline=True)
                embed.add_field(name="🔑 User Password", value=f"`{ssh_password}`", inline=True)
                embed.add_field(name="🔑 Root Password", value=f"`{root_password}`", inline=True)
            
            if tmate_session:
                embed.add_field(name="🔗 SSH Session", value=f"`{tmate_session}`", inline=False)
            
            embed.add_field(name="📝 Commands", value=f"Use `/connect_vps {token}` to connect to your VPS", inline=False)
            embed.add_field(name="⚠️ Important", value="Keep your token and passwords secure! Do not share them.", inline=False)
            
            embed.set_footer(text="CloudNode VPS Service")
            
            await user.send(embed=embed)
        except discord.Forbidden:
            await ctx.send(f"❌ Could not send DM to {user.mention}. Please enable DMs from server members.")
        
        await status_msg.edit(content=f"✅ CloudNode VPS created successfully for {user.mention}! Check your DMs for details.")
        
    except Exception as e:
        error_msg = f"Failed to create VPS: {str(e)}"
        logger.error(error_msg)
        await status_msg.edit(content=f"❌ {error_msg}")
        # Clean up any created container
        try:
            if 'container' in locals():
                container.stop()
                container.remove(force=True)
        except:
            pass

@bot.hybrid_command(name='connect_vps', description='Connect to your VPS using token')
@app_commands.describe(
    token="Your VPS access token"
)
async def connect_vps(ctx, token: str):
    """Connect to a VPS using token"""
    if bot.db.is_user_banned(ctx.author.id):
        await ctx.send("❌ You are banned from accessing VPS instances!", ephemeral=True)
        return
    
    vps_data = bot.db.get_vps_by_token(token)
    if not vps_data:
        await ctx.send("❌ Invalid token or VPS not found!", ephemeral=True)
        return
    
    if str(ctx.author.id) != vps_data['created_by'] and not has_admin_role(ctx):
        await ctx.send("❌ You don't have permission to access this VPS!", ephemeral=True)
        return
    
    try:
        container = bot.docker_client.containers.get(vps_data['container_id'])
        if container.status != "running":
            await ctx.send("🔄 Starting VPS...", ephemeral=True)
            container.start()
            await asyncio.sleep(5)
        
        # Get or create tmate session
        if not vps_data['tmate_session']:
            await ctx.send("🔄 Setting up SSH session...", ephemeral=True)
            try:
                tmate_process = await asyncio.create_subprocess_exec(
                    "docker", "exec", container.id, "sudo", "-u", vps_data['username'], "tmate", "-S", "/tmp/tmate.sock", "new-session", "-d",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.sleep(3)
                
                tmate_web_process = await asyncio.create_subprocess_exec(
                    "docker", "exec", container.id, "sudo", "-u", vps_data['username'], "tmate", "-S", "/tmp/tmate.sock", "wait", "tmate-ready",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await tmate_web_process.wait()
                
                tmate_show_process = await asyncio.create_subprocess_exec(
                    "docker", "exec", container.id, "sudo", "-u", vps_data['username'], "tmate", "-S", "/tmp/tmate.sock", "display", "-p", "#{{tmate_ssh}}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                tmate_output, _ = await tmate_show_process.communicate()
                tmate_session = tmate_output.decode().strip()
                
                bot.db.update_vps(token, {'tmate_session': tmate_session})
            except Exception as e:
                logger.error(f"Error setting up tmate: {e}")
                tmate_session = None
        else:
            tmate_session = vps_data['tmate_session']
        
        embed = discord.Embed(
            title=f"🔗 Connect to CloudNode VPS `{vps_data['vps_id']}`",
            color=discord.Color.blue(),
            timestamp=datetime.datetime.now()
        )
        
        embed.add_field(name="🔑 VPS ID", value=f"`{vps_data['vps_id']}`", inline=True)
        embed.add_field(name="💾 Memory", value=f"{vps_data['memory']}GB", inline=True)
        embed.add_field(name="⚡ CPU Cores", value=f"{vps_data['cpu']}", inline=True)
        
        if vps_data['use_custom_image']:
            embed.add_field(name="👤 Username", value=vps_data['username'], inline=True)
            embed.add_field(name="🔑 Password", value=f"`{vps_data['password']}`", inline=True)
        
        if tmate_session:
            embed.add_field(name="🔗 SSH Session", value=f"```{tmate_session}```", inline=False)
            embed.add_field(name="📝 Usage", value="Copy the SSH command above and paste it in your terminal", inline=False)
        else:
            embed.add_field(name="⚠️ Note", value="SSH session could not be established. The VPS might be restarting.", inline=False)
        
        embed.add_field(name="🔧 Management", value=f"Use `/manage_vps {vps_data['vps_id']}` to manage your VPS", inline=False)
        embed.set_footer(text="CloudNode VPS Service")
        
        await ctx.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        error_msg = f"Failed to connect to VPS: {str(e)}"
        logger.error(error_msg)
        await ctx.send(f"❌ {error_msg}", ephemeral=True)

@bot.hybrid_command(name='list', description='List all your VPS instances')
async def list_vps(ctx):
    """List all VPS instances owned by the user"""
    if bot.db.is_user_banned(ctx.author.id):
        await ctx.send("❌ You are banned from accessing VPS instances!", ephemeral=True)
        return
    
    vps_list = bot.db.get_user_vps(ctx.author.id)
    
    if not vps_list:
        embed = discord.Embed(
            title="📋 Your CloudNode VPS Instances",
            description="You don't have any VPS instances yet.",
            color=discord.Color.blue()
        )
        embed.add_field(name="Get Started", value="Ask an admin to create a VPS for you using `/create_vps`", inline=False)
        await ctx.send(embed=embed)
        return
    
    embed = discord.Embed(
        title="📋 Your CloudNode VPS Instances",
        color=discord.Color.blue(),
        timestamp=datetime.datetime.now()
    )
    
    for vps in vps_list:
        status = "🟢 Running" if vps['status'] == 'running' else "🔴 Stopped" if vps['status'] == 'stopped' else "🟡 Suspended"
        
        vps_info = (
            f"**Status:** {status}\n"
            f"**Memory:** {vps['memory']}GB\n"
            f"**CPU:** {vps['cpu']} cores\n"
            f"**Disk:** {vps['disk']}GB\n"
            f"**OS:** {vps['os_image']}\n"
            f"**Created:** {datetime.datetime.fromisoformat(vps['created_at']).strftime('%Y-%m-%d %H:%M')}\n"
            f"**Token:** `{vps['token']}`"
        )
        
        embed.add_field(
            name=f"🖥️ VPS `{vps['vps_id']}`",
            value=vps_info,
            inline=True
        )
    
    embed.set_footer(text=f"Total VPS: {len(vps_list)}")
    await ctx.send(embed=embed)

@bot.hybrid_command(name='vps_list', description='List all VPS instances (Admin only)')
async def admin_vps_list(ctx):
    """List all VPS instances (admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    
    all_vps = bot.db.get_all_vps()
    
    if not all_vps:
        await ctx.send("📭 No VPS instances found.")
        return
    
    embed = discord.Embed(
        title="📋 All CloudNode VPS Instances",
        color=discord.Color.gold(),
        timestamp=datetime.datetime.now()
    )
    
    for token, vps in list(all_vps.items())[:10]:  # Show first 10 to avoid embed limits
        try:
            owner = await bot.fetch_user(int(vps['created_by']))
            owner_name = f"{owner.name}#{owner.discriminator}"
        except:
            owner_name = f"Unknown ({vps['created_by']})"
        
        status = "🟢 Running" if vps['status'] == 'running' else "🔴 Stopped" if vps['status'] == 'stopped' else "🟡 Suspended"
        
        vps_info = (
            f"**Owner:** {owner_name}\n"
            f"**Status:** {status}\n"
            f"**Memory:** {vps['memory']}GB\n"
            f"**CPU:** {vps['cpu']} cores\n"
            f"**Created:** {datetime.datetime.fromisoformat(vps['created_at']).strftime('%Y-%m-%d %H:%M')}"
        )
        
        embed.add_field(
            name=f"🖥️ {vps['vps_id']}",
            value=vps_info,
            inline=True
        )
    
    if len(all_vps) > 10:
        embed.add_field(
            name="ℹ️ Note",
            value=f"Showing 10 out of {len(all_vps)} VPS instances",
            inline=False
        )
    
    await ctx.send(embed=embed)

@bot.hybrid_command(name='delete_vps', description='Delete a VPS instance (Admin only)')
@app_commands.describe(
    vps_id="VPS ID to delete"
)
async def delete_vps(ctx, vps_id: str):
    """Delete a VPS instance"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to delete VPS instances!", ephemeral=True)
        return
    
    token, vps_data = bot.db.get_vps_by_id(vps_id)
    if not vps_data:
        await ctx.send("❌ VPS not found!", ephemeral=True)
        return
    
    try:
        # Stop and remove container
        container = bot.docker_client.containers.get(vps_data['container_id'])
        container.stop()
        container.remove(force=True)
        
        # Remove from database
        bot.db.remove_vps(token)
        
        embed = discord.Embed(
            title="✅ VPS Deleted",
            description=f"VPS `{vps_id}` has been successfully deleted.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        
    except Exception as e:
        error_msg = f"Failed to delete VPS: {str(e)}"
        logger.error(error_msg)
        await ctx.send(f"❌ {error_msg}")

@bot.hybrid_command(name='admin_stats', description='Show system statistics (Admin only)')
async def admin_stats(ctx):
    """Show system statistics"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    
    try:
        # System stats
        stats = bot.system_stats
        
        # Bot stats
        total_vps = len(bot.db.get_all_vps())
        running_vps = len([vps for vps in bot.db.get_all_vps().values() if vps['status'] == 'running'])
        total_users = len(set(vps['created_by'] for vps in bot.db.get_all_vps().values()))
        
        # Docker stats
        if bot.docker_client:
            containers = bot.docker_client.containers.list(all=True)
            docker_stats = {
                'total': len(containers),
                'running': len([c for c in containers if c.status == 'running']),
                'stopped': len([c for c in containers if c.status == 'exited']),
                'paused': len([c for c in containers if c.status == 'paused'])
            }
        else:
            docker_stats = {'total': 0, 'running': 0, 'stopped': 0, 'paused': 0}
        
        embed = discord.Embed(
            title="📊 CloudNode System Statistics",
            color=discord.Color.green(),
            timestamp=datetime.datetime.now()
        )
        
        # System Information
        embed.add_field(
            name="🖥️ System Resources",
            value=(
                f"**CPU Usage:** {stats['cpu_usage']:.1f}%\n"
                f"**Memory:** {stats['memory_used']:.1f}/{stats['memory_total']:.1f} GB ({stats['memory_usage']:.1f}%)\n"
                f"**Disk:** {stats['disk_used']:.1f}/{stats['disk_total']:.1f} GB ({stats['disk_usage']:.1f}%)\n"
                f"**Network:** ↑{stats['network_sent']:.1f}MB ↓{stats['network_recv']:.1f}MB"
            ),
            inline=False
        )
        
        # VPS Statistics
        embed.add_field(
            name="📦 VPS Statistics",
            value=(
                f"**Total VPS:** {total_vps}\n"
                f"**Running VPS:** {running_vps}\n"
                f"**Unique Users:** {total_users}\n"
                f"**Max Containers:** {bot.db.get_setting('max_containers', MAX_CONTAINERS)}"
            ),
            inline=True
        )
        
        # Docker Statistics
        embed.add_field(
            name="🐳 Docker Statistics",
            value=(
                f"**Total Containers:** {docker_stats['total']}\n"
                f"**Running:** {docker_stats['running']}\n"
                f"**Stopped:** {docker_stats['stopped']}\n"
                f"**Paused:** {docker_stats['paused']}"
            ),
            inline=True
        )
        
        # Usage Statistics
        total_created = bot.db.get_stat('total_vps_created', 0)
        embed.add_field(
            name="📈 Usage Statistics",
            value=f"**Total VPS Created:** {total_created}",
            inline=False
        )
        
        embed.set_footer(text="CloudNode VPS Service")
        await ctx.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error in admin_stats: {e}")
        await ctx.send("❌ An error occurred while fetching statistics.")

# Additional commands and functionality would continue here...

# Error handling
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command!")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ I don't have the required permissions to execute this command!")
    else:
        logger.error(f"Command error: {error}")
        await ctx.send("❌ An unexpected error occurred while processing your command.")

# Run the bot
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
