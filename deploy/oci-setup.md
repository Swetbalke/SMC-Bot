# Oracle Cloud deployment (Ubuntu, Delta Demo/Testnet)

These files assume the repository is cloned to `/home/ubuntu/SMC-Bot` and that
the Ubuntu account is named `ubuntu`. The engine uses the existing
`config/bot_settings.json`; inspect its execution mode and demo-order setting
before enabling the service.

## 1. Create the VM

Use an Ubuntu image in the tenancy's home region. Oracle's current Always Free
allowance for Ampere A1 is 2 OCPUs and 12 GB RAM in total, with a minimum 47 GB
boot volume. Free capacity is not guaranteed in every region or availability
domain. Confirm the console labels the selected resources as Always Free before
creating them. Download the SSH private key and keep it on your own computer.

## 2. Connect and install the project

From Windows PowerShell, substitute the actual key path and VM public IP:

```powershell
ssh -i "C:\path\to\oracle-key.key" ubuntu@YOUR_ORACLE_IP
```

On the VM:

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
git clone https://github.com/Swetbalke/SMC-Bot.git /home/ubuntu/SMC-Bot
cd /home/ubuntu/SMC-Bot
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
mkdir -p data logs
chmod +x deploy/start_engine.sh deploy/start_dashboard.sh
```

If the GitHub repository is private, configure GitHub access on the VM using
your own GitHub account. Never put a GitHub token in a clone URL or send it in
chat.

## 3. Add Demo credentials securely

Create a **Delta Demo/Testnet** API key with the minimum read and trading
permissions needed. Do not enable withdrawals. Add the VM's public IP to that
Demo key's IP allowlist if Delta requires it. Do not use production credentials.

On the VM, create a root-only environment file and edit it locally over SSH:

```bash
sudo install -o root -g root -m 600 /dev/null /etc/smc-trading-bot.env
sudo nano /etc/smc-trading-bot.env
```

Put these lines in the file, replacing the values inside quotes:

```text
DELTA_ENV=testnet
DELTA_API_KEY="YOUR_DEMO_KEY"
DELTA_API_SECRET="YOUR_DEMO_SECRET"
```

Save and exit. Do not put these values in Git, the service unit files, or a
shell command. Confirm that `config/bot_settings.json` says `delta_demo` only
when you intend the engine to submit Demo/Testnet orders, and review symbol,
position size, loss limit, and open positions first.

## 4. Install the background services

From the project directory on the VM:

```bash
sudo cp deploy/smc-engine.service /etc/systemd/system/
sudo cp deploy/smc-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
```

After reviewing the Demo configuration and confirming the Delta Demo account
has no unwanted open positions or orders, enable and start the services:

```bash
sudo systemctl enable --now smc-engine smc-dashboard
sudo systemctl status smc-engine smc-dashboard --no-pager
```

Useful checks:

```bash
sudo journalctl -u smc-engine -f
sudo journalctl -u smc-dashboard -f
sudo systemctl restart smc-engine
sudo systemctl stop smc-engine
```

The SQLite database stays under `data/` on the VM boot volume. Arrange a
separate backup before relying on it as long-term storage.

## 5. Open the dashboard safely

Keep port 8501 closed in both Oracle ingress rules and the Ubuntu firewall. The
dashboard listens only on the VM's loopback interface. From a second Windows
PowerShell window, create an SSH tunnel:

```powershell
ssh -i "C:\path\to\oracle-key.key" -L 8501:127.0.0.1:8501 ubuntu@YOUR_ORACLE_IP
```

Keep that SSH window open, then visit `http://localhost:8501` in the browser.
This avoids exposing the settings-changing dashboard to the public internet.
Allow SSH (port 22) only from your current public IP where practical.
