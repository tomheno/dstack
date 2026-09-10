"""Patch the installed gpuhunt for Nebius RTX PRO 6000 (gpu-rtx6000, sm_120, 96GB).

Upstream gpuhunt does not know the gpu-rtx6000 platform. Two edits make it
resolvable: register RTXPRO6000 in the Nvidia constraints table, and fix the
Nebius platform-name regex that drops the trailing-less "gpu-rtx6000" and
mis-maps it to the 24GB Turing RTX6000. Pairs with the compute.py live-offer
injection. (autornd dstack-nebius-patterns skill 6.1 + 6.2)

Runs from a COPYed file, not a RUN heredoc, so it works on the legacy Docker
builder too (heredoc-in-RUN needs BuildKit).
"""

import pathlib

site = next(pathlib.Path("/dstack-server/.venv/lib").glob("python3.*/site-packages/gpuhunt"))

constraints = site / "_internal" / "constraints.py"
text = constraints.read_text()
anchor = 'NvidiaGPUInfo(name="RTX6000", memory=24, compute_capability=(7, 5)),'
assert anchor in text, "gpuhunt constraints anchor missing"
if "RTXPRO6000" not in text:
    added = anchor + '\n    NvidiaGPUInfo(name="RTXPRO6000", memory=96, compute_capability=(12, 0)),'
    constraints.write_text(text.replace(anchor, added, 1))

nebius = site / "providers" / "nebius.py"
text = nebius.read_text()
old_re = 'm = re.match(r"gpu-([^-]+)-", platform)'
assert old_re in text, "gpuhunt nebius regex anchor missing"
text = text.replace(old_re, 'm = re.match(r"gpu-([^-]+)", platform)', 1)
old_grp = "    gpu_name = m.group(1)\n"
assert old_grp in text, "gpuhunt nebius gpu_name anchor missing"
text = text.replace(
    old_grp,
    '    gpu_name = m.group(1)\n    if gpu_name == "rtx6000":\n        gpu_name = "RTXPRO6000"\n',
    1,
)
nebius.write_text(text)

assert "RTXPRO6000" in constraints.read_text(), "constraints verify failed"
assert 'r"gpu-([^-]+)"' in nebius.read_text(), "nebius regex verify failed"
print("[OK] gpuhunt rtx6000 patch applied at", site)
