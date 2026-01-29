#!/usr/bin/env python3
"""
Run this on DigitalOcean to fix the key loading issue:
python3 fix_key_loading.py
"""

import re

# Read the current file
with open('/workspace/kalshi_client.py', 'r') as f:
    content = f.read()

# The new _load_private_key method
new_method = '''    def _load_private_key(self):
        """Load RSA private key from PEM string or raw base64"""
        key_str = KALSHI_PRIVATE_KEY
        
        # Debug: print first/last chars to diagnose
        print(f"[KALSHI] Key length: {len(key_str)}")
        print(f"[KALSHI] Key starts with: {key_str[:50]}...")
        
        # Handle various escaped newline formats
        if '\\\\n' in key_str:
            key_str = key_str.replace('\\\\n', '\\n')
        if '\\\\r' in key_str:
            key_str = key_str.replace('\\\\r', '')
            
        # If key doesn't start with proper header, it's raw base64
        if not key_str.strip().startswith('-----BEGIN'):
            # Remove any spaces (common when copying from env vars)
            key_clean = key_str.replace(' ', '').replace('\\n', '').replace('\\t', '').strip()
            
            # Build proper PEM format with 64-char lines
            pem_lines = ["-----BEGIN RSA PRIVATE KEY-----"]
            for i in range(0, len(key_clean), 64):
                pem_lines.append(key_clean[i:i+64])
            pem_lines.append("-----END RSA PRIVATE KEY-----")
            key_str = '\\n'.join(pem_lines)
        else:
            # Already has headers - ensure proper line breaks
            lines = key_str.strip().split('\\n')
            if len(lines) == 3:  # Header, single long line, footer
                header = lines[0]
                body = lines[1].replace(' ', '')  # Remove any spaces
                footer = lines[2]
                # Split body into 64-char lines
                body_lines = [body[i:i+64] for i in range(0, len(body), 64)]
                key_str = header + '\\n' + '\\n'.join(body_lines) + '\\n' + footer
        
        print(f"[KALSHI] Processed key (first 100 chars): {key_str[:100]}...")
        
        try:
            return serialization.load_pem_private_key(
                key_str.encode(),
                password=None,
                backend=default_backend()
            )
        except Exception as e:
            print(f"[KALSHI] Failed to load key: {e}")
            print(f"[KALSHI] Full key:\\n{key_str}")
            raise'''

# Find and replace the method using regex
pattern = r'    def _load_private_key\(self\):.*?(?=\n    def |\nclass |\Z)'
content = re.sub(pattern, new_method, content, flags=re.DOTALL)

# Write back
with open('/workspace/kalshi_client.py', 'w') as f:
    f.write(content)

print("✓ Fixed kalshi_client.py!")
print("Now run: python3 -c \"from kalshi_client import KalshiClient; c = KalshiClient(); print(c.get_balance())\"")
