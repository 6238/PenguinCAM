# OnShape Extension Setup Guide

**For Team Admins & Mentors**

This guide shows how to install the PenguinCAM extension in your OnShape classroom account so students can export parts with one click.

---

## Prerequisites

- OnShape classroom account
- Admin access to manage classroom extensions ("owner", not "admin")
- PenguinCAM deployed and OAuth configured (see [Integrations Guide](INTEGRATIONS_GUIDE.md))

---

## Step 1: Create the Extension

1. **Log into OnShape** as a classroom admin
2. **Go to Company Settings** → Extensions → Integrated Extensions
3. **Click "New Extension"** or "Add Extension"
4. **Fill in the form** (see configuration below)

---

## Step 2: Extension Configuration

### **Basic Settings:**

**Name:**
```
Send to PenguinCAM
```

**Description:**
```
Send this part to PenguinCAM for CNC
```

**Location:**
```
Tree context menu
```
*This makes it appear when you right-click a part in the feature tree*

**Context:**
```
Selected part
```
*Extension only shows when a part is selected*

### **Action Settings:**

**Action URL:**
```
https://penguincam.popcornpenguins.com/onshape/import?documentId={$documentId}&workspaceId={$workspaceId}&elementId={$elementId}&partId={$partId}
```

⚠️ **IMPORTANT:** Use `{$documentId}` (dollar sign INSIDE braces), not `${documentId}`

**Action Type:**
```
Open in new window
```

### **Icon (Optional):**

Upload an SVG icon (max 100 KB) or leave blank for default

---

## Step 3: Publish to Your Company

1. **Save the extension**
2. **Subscribe your classroom** to the extension
   - Even private extensions require subscription
   - Go to App Store → My Company → Subscribe
3. **Test it!**
   - Open any Part Studio
   - Right-click a part in the feature tree
   - Look for "Send to PenguinCAM" in the Applications menu

---

## How Students Use It

### **First Time:**
1. Right-click part → "Send to PenguinCAM"
2. Sign in with team Google account (one-time)
3. Approve OnShape access (one-time)
4. Part loads automatically in PenguinCAM!

### **After Setup:**
1. Right-click part → "Send to PenguinCAM"
2. Part loads immediately!
3. Orient, generate, download

**That's it!** No manual DXF exports, no face selection, no URL copying.

---

## Troubleshooting

### **Extension doesn't appear in menu**

**Check:**
- Is the part selected? (Extension only shows for selected parts)
- Location set to "Tree context menu"?
- Classroom subscribed to the extension?

### **Opens but shows error**

**Check:**
- Action URL correct? (`{$documentId}` not `${documentId}`)
- PenguinCAM URL correct? (https://penguincam.popcornpenguins.com)
- OnShape OAuth configured? (See [Integrations Guide](INTEGRATIONS_GUIDE.md))

### **Authentication fails**

**Check:**
- Google OAuth configured?
- Student using team Google account?
- Allowed domains configured in PenguinCAM?

### **Part doesn't load**

**Check Railway logs:**
```
Railway Dashboard → Deployments → View Logs
```

Look for:
- DXF import messages
- OnShape API errors
- Face detection warnings

---

## Security Notes

- Extension uses OAuth 2.0 for secure authentication
- Students only see their own authenticated sessions
- No API keys exposed to client
- All requests go through your PenguinCAM server

---

## Alternative: Manual DXF Upload

If you can't install company extensions, students can still use PenguinCAM:

1. Right-click face in OnShape → Export → DXF
2. Go to https://penguincam.popcornpenguins.com
3. Upload DXF file
4. Orient, generate, download

Less convenient but fully functional!

---

## Related Documentation

- [Integrations Guide](INTEGRATIONS_GUIDE.md) - OnShape OAuth setup
- [Deployment Guide](DEPLOYMENT_GUIDE.md) - Railway deployment
- [Authentication Guide](AUTHENTICATION_GUIDE.md) - Google Workspace auth

---

**Questions?** Check Railway logs or contact your PenguinCAM administrator.
