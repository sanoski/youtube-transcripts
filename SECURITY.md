# Security Notes

## Current setup (private/Tailscale-only)

This app is bound to the Tailscale interface only — nginx listens on the Tailscale IP,
not the public IP. That means it's unreachable from the internet entirely. Only devices
on your Tailscale mesh can access it. This is strong protection for personal tools.

## If you ever expose an app publicly

For any app reachable from the public internet, apply these hardening practices:

### Dedicated service user
Create a low-privilege system user to run the app instead of your personal account:
```bash
useradd --system --no-create-home --shell /usr/sbin/nologin appname
```
If the app is compromised, the attacker is stuck in a locked-down account with no
shell and no home directory.

### Serve files from /opt, not your home directory
Keep app files in `/opt/appname` owned by the service user. Your home directory
contains SSH keys, shell history, and other sensitive material you don't want
exposed if the app process is exploited.

### systemd hardening flags
Add these to the `[Service]` section of your unit file:
```ini
NoNewPrivileges=true      # process can't escalate privileges
PrivateTmp=true           # isolated /tmp, can't snoop other processes
ProtectSystem=strict      # filesystem is read-only except where you allow
ReadWritePaths=/opt/appname/data  # explicitly grant write access only where needed
```

### Firewall
Use `ufw` to block everything except SSH and the ports your app needs:
```bash
ufw default deny incoming
ufw allow ssh
ufw allow 80
ufw allow 443
ufw enable
```

### HTTPS
Use Let's Encrypt via certbot for a free SSL cert. Never serve credentials or
personal data over plain HTTP on a public interface.
```bash
apt install certbot python3-certbot-nginx
certbot --nginx -d yourdomain.com
```
