#!/usr/bin/env bash
# Scan the repo for values that should not be committed. Run from the repo root.
# Exits 1 if anything looks wrong, so it can also run as a pre-commit hook.
set -u
cd "$(dirname "$0")/.." || exit 1

patterns=(
  'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'      # JWTs (Supabase anon / service_role keys)
  'sb_secret_[A-Za-z0-9_-]+'                       # Supabase secret keys
  'sk-[A-Za-z0-9_-]{20,}'                          # common API key shape
  'service_role'                                   # should never appear in client code
  '[A-Za-z0-9-]+\.app\.n8n\.cloud'                 # your n8n instance name
  'n8n\.cloud/webhook/[A-Za-z0-9-]+'               # live webhook URLs
  'Bearer [A-Za-z0-9._-]{20,}'                     # hard-coded bearer tokens
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' # email addresses
  '\b[0-9a-f]{32}\b'                               # 32-character hex tokens
  '\b[0-9a-f]{40}\b'                               # 40-character hex keys (Serper, others)
  'tok: ?.[A-Za-z0-9]{12,}'                        # token literals in n8n Config nodes
)

found=0
for p in "${patterns[@]}"; do
  hits=$(grep -rnE --exclude-dir=.git --exclude-dir=node_modules --exclude='*.pdf' --exclude='check_secrets.sh' -e "$p" . 2>/dev/null | grep -vE '@(x|a|b|example)\.(com|org)|YOUR_')
  if [ -n "$hits" ]; then
    echo "== pattern: $p"
    echo "$hits"
    found=1
  fi
done

if [ "$found" -eq 0 ]; then echo "Clean: no secret-like values found."; else echo; echo "Review the matches above before committing."; fi
exit $found
