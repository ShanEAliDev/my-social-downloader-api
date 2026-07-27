# Server Setup Guide

## Environment Variables

### Required for TikTok Downloads

| Variable | Description |
|----------|-------------|
| `TIKTOK_COOKIES_B64` | Base64-encoded TikTok cookies from `www.tiktok.com_cookies.txt` |

### Optional (already supported)

| Variable | Description |
|----------|-------------|
| `YOUTUBE_COOKIES_B64` | Base64-encoded YouTube cookies |
| `INSTAGRAM_COOKIES_B64` | Base64-encoded Instagram cookies |
| `API_KEY` | API authentication key (if set, all requests must include `X-API-Key` header) |
| `ALLOWED_ORIGINS` | Comma-separated allowed CORS origins (default: `*`) |
| `MAX_DOWNLOAD_SIZE_MB` | Maximum download size in MB (default: `500`) |
| `DOWNLOAD_CONCURRENCY` | Max concurrent downloads (default: `1`) |
| `DOWNLOAD_EXPIRY_SECONDS` | Auto-delete completed files after N seconds (default: `1500` = 25 min) |
| `DISK_SAFETY_BUFFER_MB` | Extra disk space buffer in MB (default: `100`) |
| `METADATA_CACHE_SECONDS` | Metadata cache TTL in seconds (default: `300`) |
| `MAX_FILE_AGE_HOURS` | Delete old incomplete files after N hours (default: `2`) |

---

## How to Add TikTok Cookies on Server

### Step 1: Prepare your cookies file

You already have `www.tiktok.com_cookies.txt` locally. Keep this file updated with fresh cookies from your browser.

### Step 2: Base64-encode the file

**On Linux/macOS:**
```bash
base64 -w0 www.tiktok.com_cookies.txt
```

**On Windows PowerShell:**
```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("www.tiktok.com_cookies.txt"))
```

**On Windows CMD:**
```cmd
certutil -encode www.tiktok.com_cookies.txt cookies_b64.txt
type cookies_b64.txt | findstr /v "CertUtil" > cookies_clean.txt
type cookies_clean.txt
```

### Step 3: Set the environment variable

**Railway:**
1. Go to your project → Settings → Variables
2. Add new variable:
   - Name: `TIKTOK_COOKIES_B64`
   - Value: Paste the base64 string from Step 2
3. Redeploy

**Docker:**
```bash
docker run -e TIKTOK_COOKIES_B64="PASTE_BASE64_HERE" ...
```

**Linux/macOS systemd:**
```bash
# /etc/systemd/system/your-service.d/env.conf
[Service]
Environment="TIKTOK_COOKIES_B64=PASTE_BASE64_HERE"
```

**PM2 ecosystem file:**
```javascript
module.exports = {
  apps: [{
    name: "downloader",
    script: "uvicorn",
    args: "main:app --host 0.0.0.0 --port $PORT",
    env: {
      TIKTOK_COOKIES_B64: "PASTE_BASE64_HERE"
    }
  }]
}
```

**Environment file (.env):**
```env
TIKTOK_COOKIES_B64=PASTE_BASE64_HERE
```

---

## How to Get Fresh TikTok Cookies

1. Open browser (Chrome/Firefox) and log into `tiktok.com`
2. Install cookie exporter extension:
   - Chrome: "Get cookies.txt LOCALLY"
   - Firefox: "cookies.txt"
3. Go to `tiktok.com` → Export cookies → Save as `www.tiktok.com_cookies.txt`
4. Update the base64 env var with the new file content
5. Redeploy/restart server

**Cookie validity:** TikTok cookies expire. Update the env var every few weeks or when downloads start failing.

---

## Verification

After setting the environment variable and restarting:

1. Check server logs for:
   ```
   Wrote cookies for tiktok -> /path/to/cookies/tiktok.txt
   ```

2. Test metadata endpoint:
   ```bash
   curl -X POST "https://your-api.com/get-metadata" \
     -H "Content-Type: application/json" \
     -d '{"url": "https://www.tiktok.com/@usman_offical76/video/7543977269371424017"}'
   ```

3. Test video download:
   ```bash
   curl -X POST "https://your-api.com/download" \
     -H "Content-Type: application/json" \
     -d '{"url": "https://www.tiktok.com/@usman_offical76/video/7543977269371424017", "media_type": "video"}'
   ```

4. Test audio download:
   ```bash
   curl -X POST "https://your-api.com/download" \
     -H "Content-Type: application/json" \
     -d '{"url": "https://www.tiktok.com/@usman_offical76/video/7543977269371424017", "media_type": "audio"}'
   ```

---

## Troubleshooting

### "Unable to extract universal data for rehydration"
- Your TikTok cookies are expired or invalid
- Export fresh cookies from browser and update `TIKTOK_COOKIES_B64`

### "No space left on device"
- Server disk is full
- The API automatically deletes old completed files
- Increase `DISK_SAFETY_BUFFER_MB` or decrease `MAX_DOWNLOAD_SIZE_MB`

### Downloads work locally but fail on server
- Verify `TIKTOK_COOKIES_B64` is set on server
- Check server logs for cookie loading messages
- Ensure server has outbound internet access

---

## Security Notes

- Never commit cookie files to git
- Never expose `TIKTOK_COOKIES_B64` in client-side code
- The cookie file is created at runtime with `0o600` permissions
- Cookies are ephemeral and disappear on server restart/redeploy
- Use a dedicated TikTok account for downloads, not your personal account
