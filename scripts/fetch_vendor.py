#!/usr/bin/env python3
"""Download the web page's JavaScript/CSS libraries into rag/static/vendor.

The page must work offline (the local demo), so the libraries are served
by the app instead of a CDN. Tarballs come from the npm registry, pinned
by version and SHA-256. The Docker build runs this; run it once yourself
for development outside Docker.
"""

import hashlib
import io
import tarfile
import urllib.request
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "rag" / "static" / "vendor"

PACKAGES = [
    # (tarball URL, sha256, {path in tarball (prefix): destination under vendor/})
    (
        "https://registry.npmjs.org/katex/-/katex-0.19.1.tgz",
        "24da5a599eb2fcede2e270b99a6ff48bca816beecc95feeb84e8cccbd1bb34fd",
        {
            "package/dist/katex.min.js": "katex/katex.min.js",
            "package/dist/katex.min.css": "katex/katex.min.css",
            "package/dist/fonts/": "katex/fonts/",
        },
    ),
    (
        "https://registry.npmjs.org/marked/-/marked-12.0.2.tgz",
        "6cfd2d09c6bce2541558a1547e5f3b9895ed743f0d287536ff2280e318e8a074",
        {"package/marked.min.js": "marked.min.js"},
    ),
    (
        "https://registry.npmjs.org/dompurify/-/dompurify-3.4.17.tgz",
        "056cbdae276897208b88c21d1d779adfa09ad7be488a2f510a28e900d3bbffe7",
        {"package/dist/purify.min.js": "purify.min.js"},
    ),
]


def main() -> None:
    for url, sha256, files in PACKAGES:
        data = urllib.request.urlopen(url, timeout=60).read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != sha256:
            raise SystemExit(f"checksum mismatch for {url}: {digest}")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                for src, dst in files.items():
                    if member.isfile() and (member.name == src or (src.endswith("/") and member.name.startswith(src))):
                        out = VENDOR / (dst + member.name[len(src) :] if src.endswith("/") else dst)
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(tar.extractfile(member).read())
        print(f"vendored {url.rsplit('/', 1)[1]}")


if __name__ == "__main__":
    main()
