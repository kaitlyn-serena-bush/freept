# FreePT key manager (Windows)
# Run it with "FreePT Keys.cmd". On other systems, set environment variables with the same names instead.
# Stores API keys in Windows Credential Manager, under your Windows login.
# Nothing is sent anywhere except the "Test" calls, which go straight to each
# provider's own API.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Here     = Split-Path -Parent $MyInvocation.MyCommand.Path

function Bearer($k) { @{ Authorization = "Bearer $k" } }

# Name, env var, where to get the key, how to test it
$Providers = @(
  @{ Name='Google Gemini';  Var='GEMINI_API_KEY';       Url='https://aistudio.google.com/apikey';
     Test={ param($k) Invoke-RestMethod 'https://generativelanguage.googleapis.com/v1beta/models' -Headers @{ 'x-goog-api-key'=$k } } }
  @{ Name='Groq';           Var='GROQ_API_KEY';         Url='https://console.groq.com/keys';
     Test={ param($k) Invoke-RestMethod 'https://api.groq.com/openai/v1/models' -Headers (Bearer $k) } }
  @{ Name='OpenRouter';     Var='OPENROUTER_API_KEY';   Url='https://openrouter.ai/settings/keys';
     Test={ param($k) Invoke-RestMethod 'https://openrouter.ai/api/v1/key' -Headers (Bearer $k) } }
  @{ Name='Mistral';        Var='MISTRAL_API_KEY';      Url='https://console.mistral.ai/api-keys';
     Test={ param($k) Invoke-RestMethod 'https://api.mistral.ai/v1/models' -Headers (Bearer $k) } }
  @{ Name='Cloudflare account ID'; Var='CLOUDFLARE_ACCOUNT_ID'; Url='https://dash.cloudflare.com/';
     Test=$null }
  @{ Name='Cloudflare API token';  Var='CLOUDFLARE_API_TOKEN';  Url='https://dash.cloudflare.com/profile/api-tokens';
     Test={ param($k) Invoke-RestMethod 'https://api.cloudflare.com/client/v4/user/tokens/verify' -Headers (Bearer $k) } }
  @{ Name='Cerebras';       Var='CEREBRAS_API_KEY';     Url='https://cloud.cerebras.ai/';
     Test={ param($k) Invoke-RestMethod 'https://api.cerebras.ai/v1/models' -Headers (Bearer $k) } }
  @{ Name='NVIDIA';         Var='NVIDIA_API_KEY';       Url='https://build.nvidia.com/';
     Test={ param($k) Invoke-RestMethod 'https://integrate.api.nvidia.com/v1/models' -Headers (Bearer $k) } }
  @{ Name='Cohere';         Var='COHERE_API_KEY';       Url='https://dashboard.cohere.com/api-keys';
     Test={ param($k) Invoke-RestMethod 'https://api.cohere.com/v1/models' -Headers (Bearer $k) } }
  @{ Name='SambaNova';      Var='SAMBANOVA_API_KEY';    Url='https://cloud.sambanova.ai/apis';
     Test={ param($k) Invoke-RestMethod 'https://api.sambanova.ai/v1/models' -Headers (Bearer $k) } }
)

# Windows Credential Manager (Control Panel > Credential Manager > Windows Credentials).
# Each key is a Generic credential named "FreePT:<VAR>", protected by your Windows login.
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class FreePTCred {
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  struct CREDENTIAL {
    public int Flags; public int Type; public string TargetName; public string Comment;
    public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
    public int CredentialBlobSize; public IntPtr CredentialBlob; public int Persist;
    public int AttributeCount; public IntPtr Attributes; public string TargetAlias; public string UserName;
  }
  [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern bool CredWriteW(ref CREDENTIAL c, int flags);
  [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern bool CredReadW(string target, int type, int flags, out IntPtr c);
  [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern bool CredDeleteW(string target, int type, int flags);
  [DllImport("advapi32.dll")] static extern void CredFree(IntPtr p);
  public static void Write(string target, string secret) {
    byte[] b = System.Text.Encoding.Unicode.GetBytes(secret);
    CREDENTIAL c = new CREDENTIAL();
    c.Type = 1; c.TargetName = target; c.UserName = "FreePT"; c.Persist = 2;
    c.CredentialBlobSize = b.Length; c.CredentialBlob = Marshal.AllocHGlobal(b.Length);
    Marshal.Copy(b, 0, c.CredentialBlob, b.Length);
    try { if (!CredWriteW(ref c, 0)) throw new System.ComponentModel.Win32Exception(); }
    finally { Marshal.FreeHGlobal(c.CredentialBlob); }
  }
  public static string Read(string target) {
    IntPtr p;
    if (!CredReadW(target, 1, 0, out p)) return null;
    try {
      CREDENTIAL c = (CREDENTIAL)Marshal.PtrToStructure(p, typeof(CREDENTIAL));
      if (c.CredentialBlobSize == 0) return "";
      return Marshal.PtrToStringUni(c.CredentialBlob, c.CredentialBlobSize / 2);
    } finally { CredFree(p); }
  }
  public static void Delete(string target) { CredDeleteW(target, 1, 0); }
}
"@

function Load-Keys {
  $out = @{}
  foreach ($p in $Providers) {
    $v = [FreePTCred]::Read("FreePT:$($p.Var)")
    if ($v) { $out[$p.Var] = $v }
  }
  return $out
}

function Save-Keys($map) {
  foreach ($p in $Providers) {
    $t = "FreePT:$($p.Var)"
    if ($map.ContainsKey($p.Var)) { [FreePTCred]::Write($t, $map[$p.Var]) }
    else { [FreePTCred]::Delete($t) }
  }
}

# ---------- UI ----------
$form = New-Object Windows.Forms.Form
$form.Text = 'FreePT keys'
$form.Size = New-Object Drawing.Size(860, 760)
$form.StartPosition = 'CenterScreen'
$form.Font = New-Object Drawing.Font('Segoe UI', 9.5)

$grid = New-Object Windows.Forms.TableLayoutPanel
$grid.Dock = 'Fill'
$grid.AutoScroll = $true
$grid.ColumnCount = 5
$grid.Padding = New-Object Windows.Forms.Padding(10)
[void]$grid.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Absolute', 170)))
[void]$grid.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Absolute', 70)))
[void]$grid.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Percent', 100)))
[void]$grid.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Absolute', 60)))
[void]$grid.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Absolute', 150)))

$saved = Load-Keys
$rows = @()

foreach ($p in $Providers) {
  $lbl = New-Object Windows.Forms.Label
  $lbl.Text = $p.Name; $lbl.AutoSize = $true; $lbl.Anchor = 'Left'

  $link = New-Object Windows.Forms.LinkLabel
  $link.Text = 'Get key'; $link.AutoSize = $true; $link.Anchor = 'Left'
  $link.Tag = $p.Url
  $link.Add_LinkClicked({ param($s, $e) Start-Process $s.Tag })

  $box = New-Object Windows.Forms.TextBox
  $box.Dock = 'Fill'; $box.UseSystemPasswordChar = $true
  if ($saved.ContainsKey($p.Var)) { $box.Text = $saved[$p.Var] }

  $status = New-Object Windows.Forms.Label
  $status.AutoSize = $true; $status.Anchor = 'Left'
  if ($box.Text) { $status.Text = 'Saved'; $status.ForeColor = 'DimGray' }

  $btn = New-Object Windows.Forms.Button
  $btn.Text = 'Test'; $btn.Width = 55
  if (-not $p.Test) { $btn.Enabled = $false }
  $btn.Tag = @{ P = $p; Box = $box; Status = $status }
  $btn.Add_Click({ param($s, $e) Test-Row $s.Tag })

  $grid.Controls.AddRange(@($lbl, $link, $box, $btn, $status))
  $rows += @{ P = $p; Box = $box; Status = $status }
}

function Test-Row($r) {
  $k = $r.Box.Text.Trim()
  if (-not $k) { $r.Status.Text = 'No key'; $r.Status.ForeColor = 'DimGray'; return }
  $r.Status.Text = 'Testing...'; $r.Status.ForeColor = 'DimGray'; $form.Refresh()
  try {
    [void](& $r.P.Test $k)
    $r.Status.Text = 'Works'; $r.Status.ForeColor = 'Green'
  } catch {
    $code = ''
    if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
    $r.Status.Text = "Failed $code"; $r.Status.ForeColor = 'Firebrick'
  }
}

$bar = New-Object Windows.Forms.FlowLayoutPanel
$bar.Dock = 'Bottom'; $bar.Height = 48; $bar.Padding = New-Object Windows.Forms.Padding(10, 8, 10, 8)

$show = New-Object Windows.Forms.CheckBox
$show.Text = 'Show keys'; $show.AutoSize = $true
$show.Add_CheckedChanged({ foreach ($r in $rows) { $r.Box.UseSystemPasswordChar = -not $show.Checked } })

$saveBtn = New-Object Windows.Forms.Button
$saveBtn.Text = 'Save'; $saveBtn.Width = 90
$saveBtn.Add_Click({
  $map = @{}
  foreach ($r in $rows) { $v = $r.Box.Text.Trim(); if ($v) { $map[$r.P.Var] = $v } }
  Save-Keys $map
  foreach ($r in $rows) { if ($r.Box.Text.Trim()) { if ($r.Status.Text -notin @('Works') -and $r.Status.Text -notlike 'Failed*') { $r.Status.Text = 'Saved'; $r.Status.ForeColor = 'DimGray' } } else { $r.Status.Text = '' } }
  [Windows.Forms.MessageBox]::Show("Saved $($map.Count) keys, in Windows Credential Manager.", 'FreePT') | Out-Null
})

$testAll = New-Object Windows.Forms.Button
$testAll.Text = 'Test all'; $testAll.Width = 90
$testAll.Add_Click({ foreach ($r in $rows) { if ($r.P.Test) { Test-Row $r } } })

$note = New-Object Windows.Forms.Label
$note.AutoSize = $true; $note.ForeColor = 'DimGray'
$note.Text = 'Clear a box and Save to remove that key.'
$note.Padding = New-Object Windows.Forms.Padding(10, 6, 0, 0)

$bar.Controls.AddRange(@($saveBtn, $testAll, $show, $note))
$form.Controls.Add($grid)
$form.Controls.Add($bar)
[void]$form.ShowDialog()
