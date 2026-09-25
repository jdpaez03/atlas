# ATLAS server on Linux

ATLAS can run 24/7 on a spare laptop running **Ubuntu Server 24.04 LTS**. You open it from your phone or PC over
Tailscale, a private network with no open ports. The reference machine is a Lenovo IdeaPad 320: i5-7200U, 12 GB RAM,
SSD, ethernet. Any x86-64 machine with 8 GB or more works.

What changes compared with the Windows PC:

|                         | Windows PC                         | Linux server                                                                                                   |
| ----------------------- | ---------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Work files              | the OneDrive client syncs a folder | **Microsoft Graph**: `onedrive:/` and `sharepoint:` read roots (§ Files)                                       |
| Agents' browser (REDI…) | Chrome with a profile folder       | Chrome on a virtual display (Xvfb). The login is carried over with `export-session` / `import-session`.        |
| Microsoft token cache   | encrypted with DPAPI               | a file only the ATLAS user can read (folder 700, file 600)                                                     |
| PDFs (SCRIBE)           | Segoe UI                           | Selawik, an open font with Segoe UI metrics. The decks keep naming Segoe UI, so they look the same on your PC. |
| Start                   | `start.ps1`                        | systemd services `atlas-xvfb`, `atlas-api`, `atlas-web`, started on boot                                       |
| Access                  | http://localhost:3000              | `https://<machine>.<tailnet>.ts.net`, from any of your devices on Tailscale                                    |

## 1. Install Ubuntu Server

This erases the disk. Copy anything you want off the laptop first.

1. On your PC, download **Ubuntu Server 24.04 LTS** from ubuntu.com/download/server. Write it to a USB stick of
   8 GB or more with **Rufus** (partition scheme GPT; defaults otherwise).
2. On the laptop, plug in a **USB keyboard** (the built-in one may be broken) and the ethernet cable.
3. Boot from the USB stick. On an IdeaPad, with the laptop off, press the small **Novo** pinhole button next to the
   power button, or power on while tapping **F12** (Fn+F12). Then choose the USB stick under **Boot Menu**.
4. In the installer:
   - Language and keyboard: Spanish (Latin American) if that is your keyboard.
   - Network: the ethernet port gets an address automatically; leave it.
   - Storage: **Use an entire disk**, the SSD.
   - Profile: your name, server name `atlas`, a username (e.g. `atlas`) and a strong password.
   - **Install OpenSSH server: yes.**
   - Featured snaps: none.
5. Reboot and remove the USB stick. Log in on the laptop once and note its address: `ip -4 a` (the `inet` of the
   ethernet interface, e.g. 192.168.1.50).

From now on, work from your PC. In PowerShell:

```powershell
ssh atlas@192.168.101.145
```

## 2. Run the installer

On the server, over SSH:

```bash
curl -fsSL https://raw.githubusercontent.com/jdpaez03/atlas/main/deploy/linux/install-server.sh | bash
```

It asks for your password (sudo), then installs these:

- git, Xvfb, fonts, Node.js 22, uv
- Google Chrome
- Tailscale and Claude Code
- the ATLAS repo in `~/atlas`, with the data in `~/atlas-local`

It also:

- builds the Command Center
- installs the systemd services
- keeps the laptop running with the lid closed, and never sleeping
- caps the battery charge at about 60%. The laptop is always plugged in, and this is the IdeaPad's conservation mode.

It is safe to run again.

When it reaches **Tailscale**, it prints a link. Open it and sign in; you can use your Microsoft or Google account.
Then:

- Install Tailscale on your **phone** and your **PC** with the same account.
- In the Tailscale admin console → **DNS**, enable **MagicDNS** and **HTTPS Certificates**. The script says so if
  they are off; enable them and run it again.

At the end the script prints the address, e.g. `https://atlas.tail1234.ts.net`. Only your own devices on Tailscale
can reach it. It is never exposed to the internet. After this step you can also SSH by name: `ssh atlas@atlas`
(Tailscale SSH).

## 3. Bring your data from the PC

This moves your missions, memory, lessons, knowledge notes, brand kit, private prompts, `.env`, your Claude Code agents (the EOS division), and the agents'
browser logins (REDI).

1. **Close ATLAS on the PC** (its API window).
2. In PowerShell, in the repo folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\windows\pack-for-server.ps1
   scp "$HOME\Desktop\atlas-transfer.tgz" atlas@atlas:~/
   ```

3. On the server:

   ```bash
   bash ~/atlas/deploy/linux/import-from-pc.sh ~/atlas-transfer.tgz
   ```

   It restores `~/atlas-local` and keeps any previous server data next to it. It adapts `.env` to the server:
   - the OneDrive folder becomes `onedrive:/`
   - other Windows paths are commented out
   - the tailnet URLs are set

   It then imports the browser sessions and **deletes the file**.

4. Delete `atlas-transfer.tgz` from the PC's Desktop too; it contains secrets. Stop starting ATLAS on the PC: from now on the
   server is the one ATLAS. Two copies would each keep their own history.

Do this before step 4: the import replaces `.env` and `atlas-local`, which hold the sign-ins.

## 4. Sign in (once)

**Claude (the Max plan)**:

```bash
claude            # then type /login, open the link on your phone or PC, paste the code back
```

The login is stored in `~/.claude/`. ATLAS's Agent SDK uses it, the same as on your PC.

**Microsoft 365 (OneDrive, SharePoint and Outlook)**. This needs the Entra app of docs/INBOX_SETUP.md § A:

1. Create the app if you haven't yet: Entra admin center → App registrations → New registration.
   - Accounts in this organizational directory only.
   - Under **Authentication**, set **Allow public client flows** to **Yes**.
2. Add these under **API permissions** → Microsoft Graph → **Delegated**:
   - `Files.Read.All` and `Sites.Read.All`, for the files.
   - `Mail.Read`, and `Mail.ReadWrite` for Outlook drafts, for Follow-ups.

   If your organization requires admin approval, ask IT to **Grant admin consent**. All of these are read-only
   except `Mail.ReadWrite`, which only saves drafts; ATLAS never sends mail.

3. In `~/atlas/.env`, set `ATLAS_MS_CLIENT_ID` (Application ID) and `ATLAS_MS_TENANT_ID` (Directory ID).
4. Sign in:

```bash
cd ~/atlas/apps/api
uv run atlas-graph login     # shows a code: open microsoft.com/devicelogin on your phone/PC and enter it
uv run atlas-graph status    # Account: you@paga… · Files: OK
uv run atlas-graph roots     # every onedrive:/sharepoint: root -> OK
sudo systemctl restart atlas-api
```

This one sign-in covers files and mail. The Follow-ups **Connect** button asks for the same permissions.

## Files: `onedrive:` and `sharepoint:` roots

Linux has no OneDrive client, so a node's read roots can be remote. Roots are separated by `;` and can be mixed with
local folders:

```bash
# all of your OneDrive
ATLAS_FILE_ROOTS_CORPORATE=onedrive:/
# or only some folders, plus a SharePoint document library (by its name or the last part of its URL)
ATLAS_FILE_ROOTS_CORPORATE=onedrive:/PAGA;sharepoint:pagadesarrollos.sharepoint.com/sites/Finanzas/Documentos compartidos
```

- Agents see these labels in their task and use them as paths, e.g.
  `read_file onedrive:/PAGA/Estudios/Balcones.xlsx`. A relative path that isn't found locally is tried against the
  remote roots.
- `search_files` on a remote root uses Microsoft Search, which also matches **file contents**.
- `list_files` walks folders. Folders added with _Add shortcut to My files_ are followed.
- The same sandbox rules apply as for local folders:
  - only this node's roots;
  - the deepest matching root decides which node a path belongs to;
  - `.env*`, keys and similar are denied.
- Nothing is ever written to OneDrive or SharePoint.
- Files are read one at a time and cached in `atlas-local/graphcache/`. The cache is keyed by version, so an edited
  file is downloaded again. It keeps the last 400 files.
- Evidence records the label (`onedrive:/…`) as the file read, so the claim check and AUDITOR work as before.

Check the roots with `uv run atlas-graph roots` and browse them with `uv run atlas-graph ls onedrive:/PAGA`.

## Agents' browser on the server

Chrome runs **headed** on the virtual display `:99` (service `atlas-xvfb`), because some sites block headless
browsers. A Chrome profile can't be copied from Windows to Linux, so the login is moved as a session: cookies and
localStorage of the agent's allowed sites only.

```powershell
# PC (after atlas-browser login market-studies works there)
uv run atlas-browser export-session market-studies --out $HOME\Desktop\redi.json
scp $HOME\Desktop\redi.json atlas@atlas:~/
```

```bash
# server
cd ~/atlas/apps/api && DISPLAY=:99 uv run atlas-browser import-session market-studies ~/redi.json && rm ~/redi.json
```

`pack-for-server.ps1` / `import-from-pc.sh` already do this for every agent with a saved login. When the site logs
the session out, repeat it: log in on the PC, export, import.

To sign in directly on the server instead, look at its virtual screen:

1. `sudo apt install -y x11vnc`
2. On the server: `x11vnc -display :99 -localhost -once -nopw`
3. On the PC: `ssh -L 5900:localhost:5900 atlas@atlas`, then open a VNC viewer on `localhost:5900`.
4. On the server: `DISPLAY=:99 uv run atlas-browser login market-studies`

## Day to day

|            |                                                                                                              |
| ---------- | ------------------------------------------------------------------------------------------------------------ |
| Open ATLAS | `https://<machine>.<tailnet>.ts.net` (phone or PC with Tailscale on)                                         |
| Status     | `systemctl status atlas-api atlas-web atlas-xvfb`                                                            |
| Logs       | `journalctl -u atlas-api -f`                                                                                 |
| Update     | `bash ~/atlas/deploy/linux/update.sh` (pull, dependencies, rebuild, restart)                                 |
| Restart    | `sudo systemctl restart atlas-api`                                                                           |
| Backup     | `sqlite3 ~/atlas-local/atlas.db ".backup ~/atlas-backup.db"` and copy `~/atlas-local` elsewhere now and then |

- The services start on boot. The battery covers short power cuts.
- Ubuntu installs security updates by itself. Run `sudo apt upgrade` and reboot now and then.

## Troubleshooting

- **The page loads but shows the demo / "API offline"**: `journalctl -u atlas-api -n 80`. If you changed the
  address, rerun `install-server.sh`; it rebuilds the Command Center with the API URL.
- **Agents say "not signed in to Microsoft 365 for files"**: run `uv run atlas-graph login`, then
  `sudo systemctl restart atlas-api`.
- **`atlas-graph roots` says NOT FOUND**: check the path with `uv run atlas-graph ls onedrive:/`. For SharePoint,
  the error lists the site's library names.
- **"could not start a browser"**: `systemctl status atlas-xvfb`, then `google-chrome --version`.
- **Live agents unavailable**: run `claude`, then `/login` again.
- **The laptop sleeps with the lid closed**: `cat /etc/systemd/logind.conf.d/atlas-lid.conf`, then reboot once.
