---
name: Bug report
about: Create a bug report to help us improve.
title: ""
labels: bug
assignees: ""
---

**Describe the bug**
A clear and concise description of what the bug is.

**SDK version**
Output of `pip show comfy-sdk` (or `python -c "import comfy_sdk; print(comfy_sdk.__version__)"`).

**Python version**
Output of `python --version`.

**Which deployment**
Comfy Cloud / serverless / self-hosted (behind [comfy-api-proxy](https://github.com/Comfy-Org/comfy-api-proxy)) — and the proxy version if self-hosted.

**Minimal reproduction**
The smallest snippet that reproduces it. Please redact your API key.

```python
from comfy_sdk import Comfy

client = Comfy(api_key="comfyui-...")
# ...
```

**Expected behavior**
A clear and concise description of what you expected to happen.

**Actual behavior**
What happened instead, including the full traceback if there is one.

```
paste traceback here
```

**Nice to have**

- [ ] Terminal output
- [ ] The workflow JSON (or a trimmed version of it)
- [ ] Screenshots

**Additional context**
Add any other context about the problem here.
