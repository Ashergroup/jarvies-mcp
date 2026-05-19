param(
    [switch]$EnvLineOnly
)

$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$key = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')

if ($EnvLineOnly) {
    "MCP_API_KEYS=$key"
    exit 0
}

"Generated MCP API key:"
$key
""
"Add this to your .env file:"
"MCP_API_KEYS=$key"
""
"Use this HTTP header from MCP clients:"
"X-API-Key: $key"
