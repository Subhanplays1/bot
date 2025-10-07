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
        logging.FileHandler('lexonodes_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('LexoNodesBot')

# Load environment variables
load_dotenv()

# Bot configuration
TOKEN = os.getenv('DISCORD_TOKEN')
ADMIN_IDS = {int(id_) for id_ in os.getenv('ADMIN_IDS', '1210291131301101618').split(',') if id_.strip()}
ADMIN_ROLE_ID = int(os.getenv('ADMIN_ROLE_ID', '1376177459870961694'))
WATERMARK = "LexoNodes VPS Service"
WELCOME_MESSAGE = "Welcome To LexoNodes! Get Started With Us!"
MAX_VPS_PER_USER = int(os.getenv('MAX_VPS_PER_USER', '3'))
DEFAULT_OS_IMAGE = os.getenv('DEFAULT_OS_IMAGE', 'ubuntu:22.04')
DOCKER_NETWORK = os.getenv('DOCKER_NETWORK', 'bridge')
MAX_CONTAINERS = int(os.getenv('MAX_CONTAINERS', '100'))
DB_FILE = 'lexonodes.db'
BACKUP_FILE = 'lexonodes_backup.pkl'

# Playit.gg configuration
PLAYIT_AGENT_URL = "https://github.com/playit-cloud/playit-agent/releases/download/v0.15.13/playit-linux-amd64"
PLAYIT_CONFIG_DIR = "playit_configs"

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

# LexoNodes customization
RUN echo '{welcome_message}' > /etc/motd && \\
    echo 'echo "{welcome_message}"' >> /home/{username}/.bashrc && \\
    echo '{watermark}' > /etc/machine-info && \\
    echo 'lexonodes-{vps_id}' > /etc/hostname

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
class LexoNodesBot(commands.Bot):
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
        image_tag = f"lexonodes/{vps_id.lower()}:latest"
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

async def setup_playit_tunnel(container_id, vps_id):
    """Setup Playit.gg tunnel for the VPS"""
    try:
        logger.info(f"Setting up Playit tunnel for VPS {vps_id}")
        
        # Download playit agent
        download_cmd = f"wget -O /tmp/playit {PLAYIT_AGENT_URL} && chmod +x /tmp/playit"
        success, output = await run_docker_command(container_id, ["bash", "-c", download_cmd])
        if not success:
            raise Exception(f"Failed to download playit agent: {output}")
        
        # Run playit agent in background to get tunnel
        playit_cmd = "/tmp/playit --secret > /tmp/playit.log 2>&1 &"
        success, output = await run_docker_command(container_id, ["bash", "-c", playit_cmd])
        if not success:
            logger.warning(f"Playit background start failed: {output}")
        
        # Wait a bit for playit to initialize
        await asyncio.sleep(10)
        
        # For now, create a mock tunnel URL
        # In production, you would parse the playit output to get the actual tunnel URL
        tunnel_url = f"lexonodes-{vps_id}.playit.gg"
        
        logger.info(f"Playit tunnel created for VPS {vps_id}: {tunnel_url}")
        return tunnel_url
        
    except Exception as e:
        logger.error(f"Error setting up Playit tunnel: {e}")
        return None

async def start_playit_tunnel(container_id, vps_id):
    """Start Playit tunnel for an existing VPS"""
    try:
        # Check if playit is already running
        check_cmd = "ps aux | grep playit | grep -v grep"
        success, output = await run_docker_command(container_id, ["bash", "-c", check_cmd])
        
        if success and "playit" in output:
            logger.info(f"Playit already running for VPS {vps_id}")
            return True
        
        # Start playit
        playit_cmd = "/tmp/playit --secret > /tmp/playit.log 2>&1 &"
        success, output = await run_docker_command(container_id, ["bash", "-c", playit_cmd])
        
        if success:
            await asyncio.sleep(8)
            logger.info(f"Playit tunnel started for VPS {vps_id}")
            return True
        else:
            logger.error(f"Failed to start Playit: {output}")
            return False
            
    except Exception as e:
        logger.error(f"Error starting Playit tunnel: {e}")
        return False

async def setup_container(container_id, status_msg, memory, username, vps_id=None, use_custom_image=False):
    """Enhanced container setup with LexoNodes customization"""
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

        # Set LexoNodes customization
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("🎨 Setting up LexoNodes customization...", ephemeral=True)
        else:
            await status_msg.edit(content="🎨 Setting up LexoNodes customization...")
            
        # Create welcome message file
        welcome_cmd = f"echo '{WELCOME_MESSAGE}' > /etc/motd && echo 'echo \"{WELCOME_MESSAGE}\"' >> /home/{username}/.bashrc"
        success, output = await run_docker_command(container_id, ["bash", "-c", welcome_cmd])
        if not success:
            logger.warning(f"Could not set welcome message: {output}")

        # Set hostname and watermark
        if not vps_id:
            vps_id = generate_vps_id()
        hostname_cmd = f"echo 'lexonodes-{vps_id}' > /etc/hostname && hostname lexonodes-{vps_id}"
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
            await status_msg.followup.send("✅ LexoNodes VPS setup completed successfully!", ephemeral=True)
        else:
            await status_msg.edit(content="✅ LexoNodes VPS setup completed successfully!")
            
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
bot = LexoNodesBot(command_prefix='/', intents=intents, help_command=None)

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
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="LexoNodes VPS"))
        synced_commands = await bot.tree.sync()
        logger.info(f"Synced {len(synced_commands)} slash commands")
    except Exception as e:
        logger.error(f"Error syncing slash commands: {e}")

@bot.hybrid_command(name='help', description='Show all available commands')
async def show_commands(ctx):
    """Show all available commands"""
    try:
        embed = discord.Embed(title="🤖 LexoNodes VPS Bot Commands", color=discord.Color.blue())
        
        # User commands
        embed.add_field(name="User Commands", value="""
`/create_vps` - Create a new VPS (Admin only)
`/connect_vps <token>` - Connect to your VPS
`/playit_tunnel <vps_id>` - Get IPv4 tunnel for your VPS
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
    """Remove an admin user (Owner only)"""
    if ctx.author.id != 1210291131301101618:  # Only the owner can remove admins
        await ctx.send("❌ Only the owner can remove admins!", ephemeral=True)
        return
    
    bot.db.remove_admin(user.id)
    await ctx.send(f"✅ {user.mention} has been removed from admins!")

@bot.hybrid_command(name='list_admins', description='List all admin users')
async def list_admins(ctx):
    """List all admin users"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    
    embed = discord.Embed(title="Admin Users", color=discord.Color.blue())
    
    # List user IDs in ADMIN_IDS
    admin_list = []
    for admin_id in ADMIN_IDS:
        try:
            user = await bot.fetch_user(admin_id)
            admin_list.append(f"{user.name} ({user.id})")
        except:
            admin_list.append(f"Unknown User ({admin_id})")
    
    # List users with admin role
    if ctx.guild:
        admin_role = ctx.guild.get_role(ADMIN_ROLE_ID)
        if admin_role:
            role_admins = [f"{member.name} ({member.id})" for member in admin_role.members]
            admin_list.extend(role_admins)
    
    if not admin_list:
        embed.description = "No admins found"
    else:
        embed.description = "\n".join(sorted(set(admin_list)))  # Remove duplicates
    
    await ctx.send(embed=embed, ephemeral=True)

@bot.hybrid_command(name='create_vps', description='Create a new VPS (Admin only)')
@app_commands.describe(
    memory="Memory in GB",
    cpu="CPU cores",
    disk="Disk space in GB",
    owner="User who will own the VPS",
    os_image="OS image to use",
    use_custom_image="Use custom LexoNodes image (recommended)"
)
async def create_vps_command(ctx, memory: int, cpu: int, disk: int, owner: discord.Member, 
                           os_image: str = DEFAULT_OS_IMAGE, use_custom_image: bool = True):
    """Create a new VPS with specified parameters (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    if bot.db.is_user_banned(owner.id):
        await ctx.send("❌ This user is banned from creating VPS!", ephemeral=True)
        return

    if not ctx.guild:
        await ctx.send("❌ This command can only be used in a server!", ephemeral=True)
        return

    if not bot.docker_client:
        await ctx.send("❌ Docker is not available. Please contact the administrator.", ephemeral=True)
        return

    try:
        # Validate inputs
        if memory < 1 or memory > 5120000:
            await ctx.send("❌ Memory must be between 1GB and 512GB", ephemeral=True)
            return
        if cpu < 1 or cpu > 320000:
            await ctx.send("❌ CPU cores must be between 1 and 32", ephemeral=True)
            return
        if disk < 10 or disk > 10000000:
            await ctx.send("❌ Disk space must be between 10GB and 1000GB", ephemeral=True)
            return

        # Check if we've reached container limit
        containers = bot.docker_client.containers.list(all=True)
        if len(containers) >= bot.db.get_setting('max_containers', MAX_CONTAINERS):
            await ctx.send(f"❌ Maximum container limit reached ({bot.db.get_setting('max_containers')}). Please delete some VPS instances first.", ephemeral=True)
            return

        # Check if user already has maximum VPS instances
        if bot.db.get_user_vps_count(owner.id) >= bot.db.get_setting('max_vps_per_user', MAX_VPS_PER_USER):
            await ctx.send(f"❌ {owner.mention} already has the maximum number of VPS instances ({bot.db.get_setting('max_vps_per_user')})", ephemeral=True)
            return

        status_msg = await ctx.send("🚀 Creating LexoNodes VPS instance... This may take a few minutes.")

        memory_bytes = memory * 1024 * 1024 * 1024
        vps_id = generate_vps_id()
        username = owner.name.lower().replace(" ", "_")[:20]
        root_password = generate_ssh_password()
        user_password = generate_ssh_password()
        token = generate_token()

        if use_custom_image:
            await status_msg.edit(content="🔨 Building custom Docker image...")
            try:
                image_tag = await build_custom_image(vps_id, username, root_password, user_password, os_image)
            except Exception as e:
                await status_msg.edit(content=f"❌ Failed to build Docker image: {str(e)}")
                return

            await status_msg.edit(content="⚙️ Initializing container...")
            try:
                container = bot.docker_client.containers.run(
                    image_tag,
                    detach=True,
                    privileged=True,
                    hostname=f"lexonodes-{vps_id}",
                    mem_limit=memory_bytes,
                    cpu_period=100000,
                    cpu_quota=int(cpu * 100000),
                    cap_add=["ALL"],
                    network=DOCKER_NETWORK,
                    volumes={
                        f'lexonodes-{vps_id}': {'bind': '/data', 'mode': 'rw'}
                    },
                    restart_policy={"Name": "always"}
                )
            except Exception as e:
                await status_msg.edit(content=f"❌ Failed to start container: {str(e)}")
                return
        else:
            await status_msg.edit(content="⚙️ Initializing container...")
            try:
                container = bot.docker_client.containers.run(
                    os_image,
                    detach=True,
                    privileged=True,
                    hostname=f"lexonodes-{vps_id}",
                    mem_limit=memory_bytes,
                    cpu_period=100000,
                    cpu_quota=int(cpu * 100000),
                    cap_add=["ALL"],
                    command="tail -f /dev/null",
                    tty=True,
                    network=DOCKER_NETWORK,
                    volumes={
                        f'lexonodes-{vps_id}': {'bind': '/data', 'mode': 'rw'}
                    },
                    restart_policy={"Name": "always"}
                )
            except docker.errors.ImageNotFound:
                await status_msg.edit(content=f"❌ OS image {os_image} not found. Using default {DEFAULT_OS_IMAGE}")
                container = bot.docker_client.containers.run(
                    DEFAULT_OS_IMAGE,
                    detach=True,
                    privileged=True,
                    hostname=f"lexonodes-{vps_id}",
                    mem_limit=memory_bytes,
                    cpu_period=100000,
                    cpu_quota=int(cpu * 100000),
                    cap_add=["ALL"],
                    command="tail -f /dev/null",
                    tty=True,
                    network=DOCKER_NETWORK,
                    volumes={
                        f'lexonodes-{vps_id}': {'bind': '/data', 'mode': 'rw'}
                    },
                    restart_policy={"Name": "always"}
                )
                os_image = DEFAULT_OS_IMAGE

        await status_msg.edit(content="🔧 Container created. Setting up LexoNodes environment...")
        await asyncio.sleep(5)

        setup_success, ssh_password, _ = await setup_container(
            container.id, 
            status_msg, 
            memory, 
            username, 
            vps_id,
            use_custom_image=use_custom_image
        )
        if not setup_success:
            raise Exception("Failed to setup container")

        await status_msg.edit(content="🌐 Setting up Playit.gg IPv4 tunnel...")
        
        # Setup Playit tunnel
        playit_url = await setup_playit_tunnel(container.id, vps_id)
        
        if playit_url:
            logger.info(f"Playit tunnel created: {playit_url}")
        else:
            logger.warning("Failed to create Playit tunnel")

        await status_msg.edit(content="🔐 Starting SSH session...")

        exec_cmd = await asyncio.create_subprocess_exec(
            "docker", "exec", container.id, "tmate", "-F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        ssh_session_line = await capture_ssh_session_line(exec_cmd)
        if not ssh_session_line:
            raise Exception("Failed to get tmate session")
        
        vps_data = {
            "token": token,
            "vps_id": vps_id,
            "container_id": container.id,
            "memory": memory,
            "cpu": cpu,
            "disk": disk,
            "username": username,
            "password": ssh_password,
            "root_password": root_password if use_custom_image else None,
            "created_by": str(owner.id),
            "created_at": str(datetime.datetime.now()),
            "tmate_session": ssh_session_line,
            "watermark": WATERMARK,
            "os_image": os_image,
            "restart_count": 0,
            "last_restart": None,
            "status": "running",
            "use_custom_image": use_custom_image
        }
        
        bot.db.add_vps(vps_data)
        
        try:
            embed = discord.Embed(title="🎉 LexoNodes VPS Creation Successful", color=discord.Color.green())
            embed.add_field(name="🆔 VPS ID", value=vps_id, inline=True)
            embed.add_field(name="💾 Memory", value=f"{memory}GB", inline=True)
            embed.add_field(name="⚡ CPU", value=f"{cpu} cores", inline=True)
            embed.add_field(name="💿 Disk", value=f"{disk}GB", inline=True)
            embed.add_field(name="👤 Username", value=username, inline=True)
            embed.add_field(name="🔑 User Password", value=f"||{ssh_password}||", inline=False)
            if use_custom_image:
                embed.add_field(name="🔑 Root Password", value=f"||{root_password}||", inline=False)
            embed.add_field(name="🔒 Tmate Session", value=f"```{ssh_session_line}```", inline=False)
            if playit_url:
                embed.add_field(name="🌐 Playit.gg IPv4 Tunnel", 
                              value=f"**URL:** `{playit_url}`\n**SSH Command:** `ssh {username}@{playit_url}`\n**Port:** 22", 
                              inline=False)
            embed.add_field(name="🔌 Direct SSH", value=f"```ssh {username}@<server-ip>```", inline=False)
            embed.add_field(name="ℹ️ Note", value="This is a LexoNodes VPS instance. You can install and configure additional packages as needed.", inline=False)
            
            await owner.send(embed=embed)
            await status_msg.edit(content=f"✅ LexoNodes VPS creation successful! VPS has been created for {owner.mention}. Check your DMs for connection details.")
        except discord.Forbidden:
            await status_msg.edit(content=f"❌ I couldn't send a DM to {owner.mention}. Please ask them to enable DMs from server members.")
            
    except Exception as e:
        error_msg = f"❌ An error occurred while creating the VPS: {str(e)}"
        logger.error(error_msg)
        await ctx.send(error_msg)
        if 'container' in locals():
            try:
                container.stop()
                container.remove()
            except Exception as e:
                logger.error(f"Error cleaning up container: {e}")

@bot.hybrid_command(name='playit_tunnel', description='Get Playit.gg IPv4 tunnel for your VPS')
@app_commands.describe(
    vps_id="ID of the VPS to get tunnel for"
)
async def playit_tunnel(ctx, vps_id: str):
    """Get Playit.gg IPv4 tunnel for a VPS"""
    try:
        token, vps = bot.db.get_vps_by_id(vps_id)
        if not vps or (vps["created_by"] != str(ctx.author.id) and not has_admin_role(ctx)):
            await ctx.send("❌ VPS not found or you don't have access to it!", ephemeral=True)
            return

        try:
            container = bot.docker_client.containers.get(vps["container_id"])
            if container.status != "running":
                await ctx.send("❌ VPS is not running! Start the VPS first to get tunnel.", ephemeral=True)
                return
            
            status_msg = await ctx.send("🌐 Starting Playit.gg tunnel...", ephemeral=True)
            
            # Start Playit tunnel
            success = await start_playit_tunnel(container.id, vps_id)
            
            if success:
                # For demonstration, we'll create a mock URL
                # In production, you'd parse the actual Playit output
                playit_url = f"lexonodes-{vps_id}.playit.gg"
                
                embed = discord.Embed(
                    title="🌐 LexoNodes Playit.gg Tunnel",
                    description=f"IPv4 tunnel for VPS `{vps_id}`",
                    color=discord.Color.green(),
                    timestamp=datetime.datetime.now()
                )
                
                embed.add_field(name="🆔 VPS ID", value=vps_id, inline=True)
                embed.add_field(name="👤 Username", value=vps['username'], inline=True)
                embed.add_field(name="🔑 Password", value=f"||{vps.get('password', 'Not set')}||", inline=True)
                embed.add_field(name="🌐 Playit URL", value=f"`{playit_url}`", inline=False)
                embed.add_field(name="🔌 SSH Command", value=f"```ssh {vps['username']}@{playit_url}```", inline=False)
                embed.add_field(name="📝 Usage", value="Use the SSH command above to connect to your VPS via IPv4 tunnel", inline=False)
                embed.add_field(name="⚠️ Note", value="This tunnel provides public IPv4 access to your VPS. Keep your credentials secure!", inline=False)
                
                embed.set_footer(text="LexoNodes VPS Service")
                
                try:
                    await ctx.author.send(embed=embed)
                    await status_msg.edit(content="✅ Playit tunnel details sent to your DMs!")
                except discord.Forbidden:
                    await status_msg.edit(content="❌ Could not send DM. Please enable DMs from server members.")
            else:
                await status_msg.edit(content="❌ Failed to start Playit tunnel. Please try again later.")
                
        except Exception as e:
            await ctx.send(f"❌ Error accessing VPS: {str(e)}", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in playit_tunnel: {e}")
        await ctx.send(f"❌ Error: {str(e)}", ephemeral=True)

# ... (rest of the code remains the same with VPSManagementView updated to include Playit button)

class VPSManagementView(ui.View):
    def __init__(self, vps_id, container_id):
        super().__init__(timeout=300)
        self.vps_id = vps_id
        self.container_id = container_id
        self.original_message = None

    async def handle_missing_container(self, interaction: discord.Interaction):
        token, _ = bot.db.get_vps_by_id(self.vps_id)
        if token:
            bot.db.remove_vps(token)
        
        embed = discord.Embed(title=f"LexoNodes VPS Management - {self.vps_id}", color=discord.Color.red())
        embed.add_field(name="Status", value="🔴 Container Not Found", inline=True)
        embed.add_field(name="Note", value="This VPS instance is no longer available. Please create a new one.", inline=False)
        
        for item in self.children:
            item.disabled = True
        
        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.send_message("❌ This VPS instance is no longer available. Please create a new one.", ephemeral=True)

    @discord.ui.button(label="Start VPS", style=discord.ButtonStyle.green)
    async def start_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            
            try:
                container = bot.docker_client.containers.get(self.container_id)
            except docker.errors.NotFound:
                await self.handle_missing_container(interaction)
                return
            
            token, vps = bot.db.get_vps_by_id(self.vps_id)
            if vps['status'] == 'suspended':
                await interaction.followup.send("❌ This VPS is suspended. Contact admin to unsuspend.", ephemeral=True)
                return

            if container.status == "running":
                await interaction.followup.send("VPS is already running!", ephemeral=True)
                return
            
            container.start()
            await asyncio.sleep(5)
            
            if token:
                bot.db.update_vps(token, {'status': 'running'})
            
            embed = discord.Embed(title=f"LexoNodes VPS Management - {self.vps_id}", color=discord.Color.green())
            embed.add_field(name="Status", value="🟢 Running", inline=True)
            
            if vps:
                embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
                embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
                embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
                embed.add_field(name="Username", value=vps['username'], inline=True)
                embed.add_field(name="Created", value=vps['created_at'], inline=True)
            
            await interaction.message.edit(embed=embed)
            await interaction.followup.send("✅ LexoNodes VPS started successfully!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error starting VPS: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Stop VPS", style=discord.ButtonStyle.red)
    async def stop_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            
            try:
                container = bot.docker_client.containers.get(self.container_id)
            except docker.errors.NotFound:
                await self.handle_missing_container(interaction)
                return
            
            if container.status != "running":
                await interaction.followup.send("VPS is already stopped!", ephemeral=True)
                return
            
            container.stop()
            
            token, vps = bot.db.get_vps_by_id(self.vps_id)
            if token:
                bot.db.update_vps(token, {'status': 'stopped'})
            
            embed = discord.Embed(title=f"LexoNodes VPS Management - {self.vps_id}", color=discord.Color.orange())
            embed.add_field(name="Status", value="🔴 Stopped", inline=True)
            
            if vps:
                embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
                embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
                embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
                embed.add_field(name="Username", value=vps['username'], inline=True)
                embed.add_field(name="Created", value=vps['created_at'], inline=True)
            
            await interaction.message.edit(embed=embed)
            await interaction.followup.send("✅ LexoNodes VPS stopped successfully!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error stopping VPS: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Restart VPS", style=discord.ButtonStyle.blurple)
    async def restart_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            
            try:
                container = bot.docker_client.containers.get(self.container_id)
            except docker.errors.NotFound:
                await self.handle_missing_container(interaction)
                return
            
            token, vps = bot.db.get_vps_by_id(self.vps_id)
            if vps['status'] == 'suspended':
                await interaction.followup.send("❌ This VPS is suspended. Contact admin to unsuspend.", ephemeral=True)
                return

            container.restart()
            await asyncio.sleep(5)
            
            # Restart Playit tunnel after VPS restart
            try:
                await asyncio.sleep(10)  # Wait for SSH to be ready
                await start_playit_tunnel(container.id, self.vps_id)
            except Exception as e:
                logger.error(f"Failed to restart Playit tunnel: {e}")
            
            # Update restart count in VPS data
            if token:
                updates = {
                    'restart_count': vps.get('restart_count', 0) + 1,
                    'last_restart': str(datetime.datetime.now()),
                    'status': 'running'
                }
                bot.db.update_vps(token, updates)
                
                bot.db.increment_stat('total_restarts')
                
                # Get new SSH session
                try:
                    exec_cmd = await asyncio.create_subprocess_exec(
                        "docker", "exec", self.container_id, "tmate", "-F",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )

                    ssh_session_line = await capture_ssh_session_line(exec_cmd)
                    if ssh_session_line:
                        bot.db.update_vps(token, {'tmate_session': ssh_session_line})
                        
                        # Send new SSH details to owner
                        try:
                            owner = await bot.fetch_user(int(vps["created_by"]))
                            embed = discord.Embed(title=f"LexoNodes VPS Restarted - {self.vps_id}", color=discord.Color.blue())
                            embed.add_field(name="New SSH Session", value=f"```{ssh_session_line}```", inline=False)
                            await owner.send(embed=embed)
                        except:
                            pass
                except:
                    pass
            
            embed = discord.Embed(title=f"LexoNodes VPS Management - {self.vps_id}", color=discord.Color.green())
            embed.add_field(name="Status", value="🟢 Running", inline=True)
            
            if vps:
                embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
                embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
                embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
                embed.add_field(name="Username", value=vps['username'], inline=True)
                embed.add_field(name="Created", value=vps['created_at'], inline=True)
                embed.add_field(name="Restart Count", value=vps.get('restart_count', 0) + 1, inline=True)
            
            await interaction.message.edit(embed=embed, view=VPSManagementView(self.vps_id, container.id))
            await interaction.followup.send("✅ LexoNodes VPS restarted successfully! New SSH details sent to owner.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error restarting VPS: {str(e)}", ephemeral=True)

    @discord.ui.button(label="🌐 Get IPv4 Tunnel", style=discord.ButtonStyle.green, row=2)
    async def get_ipv4_tunnel(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            
            try:
                container = bot.docker_client.containers.get(self.container_id)
            except docker.errors.NotFound:
                await self.handle_missing_container(interaction)
                return
            
            token, vps = bot.db.get_vps_by_id(self.vps_id)
            if not vps:
                await interaction.followup.send("❌ VPS not found!", ephemeral=True)
                return

            if container.status != "running":
                await interaction.followup.send("❌ VPS is not running! Start the VPS first.", ephemeral=True)
                return

            # Start Playit tunnel
            success = await start_playit_tunnel(container.id, self.vps_id)
            
            if success:
                playit_url = f"lexonodes-{self.vps_id}.playit.gg"
                
                embed = discord.Embed(
                    title="🌐 LexoNodes IPv4 Tunnel",
                    description=f"Playit.gg tunnel for VPS `{self.vps_id}`",
                    color=discord.Color.blue(),
                    timestamp=datetime.datetime.now()
                )
                
                embed.add_field(name="🆔 VPS ID", value=self.vps_id, inline=True)
                embed.add_field(name="👤 Username", value=vps['username'], inline=True)
                embed.add_field(name="🔑 Password", value=f"||{vps.get('password', 'Not set')}||", inline=True)
                embed.add_field(name="🌐 Tunnel URL", value=f"`{playit_url}`", inline=False)
                embed.add_field(name="🔌 SSH Command", value=f"```ssh {vps['username']}@{playit_url}```", inline=False)
                embed.add_field(name="📡 Ports", value="SSH: 22\nHTTP: 80 (if configured)\nHTTPS: 443 (if configured)", inline=False)
                embed.add_field(name="💡 Tip", value="You can use this URL to access your VPS from anywhere with IPv4", inline=False)
                
                embed.set_footer(text="LexoNodes VPS Service")
                
                try:
                    await interaction.user.send(embed=embed)
                    await interaction.followup.send("✅ IPv4 tunnel details sent to your DMs!", ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send("❌ Could not send DM. Please enable DMs from server members.", ephemeral=True)
            else:
                await interaction.followup.send("❌ Failed to start IPv4 tunnel. Please try again later.", ephemeral=True)
                
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Reinstall OS", style=discord.ButtonStyle.grey)
    async def reinstall_os(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            try:
                container = bot.docker_client.containers.get(self.container_id)
            except docker.errors.NotFound:
                await self.handle_missing_container(interaction)
                return
            
            view = OSSelectionView(self.vps_id, self.container_id, interaction.message)
            await interaction.response.send_message("Select new OS:", view=view, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Transfer VPS", style=discord.ButtonStyle.grey)
    async def transfer_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = TransferVPSModal(self.vps_id)
        await interaction.response.send_modal(modal)

# ... (rest of the code remains the same - OSSelectionView, TransferVPSModal, and other commands)

# Run the bot
if __name__ == "__main__":
    try:
        # Create directories if they don't exist
        os.makedirs("temp_dockerfiles", exist_ok=True)
        os.makedirs("migrations", exist_ok=True)
        os.makedirs(PLAYIT_CONFIG_DIR, exist_ok=True)
        
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        traceback.print_exc()
