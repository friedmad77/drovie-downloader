"""Python components of the put.io -> Plex processor.

The processor is bash (``putio_download_and_delete.sh``) and stays the entry
point.  Concerns move in here one at a time; each phase is independently
shippable and independently revertible, and until a phase lands, the bash
implementation is still the one that runs.

Two rules constrain everything in this package:

* **Standard library only.**  There is no venv on the server and no
  requirements file, which is what makes depending on a Homebrew interpreter
  safe: with no dependencies there is nothing for a ``brew upgrade`` to orphan.
* **Never one process per item.**  Bash calls in here are batched — a whole
  listing in, a whole answer back.  A subprocess inside a per-item loop once
  cost 36 seconds of every idle run of the processor.

The bash side hands over configuration on the environment; see
:mod:`putio.config` for why the parsing never goes the other way.
"""

__all__ = ["__version__", "SCHEMA"]

__version__ = "0.1.0"

#: Version of the bash <-> Python handoff — the set of environment names in
#: ``PUTIO_PY_VARS`` and what they mean.  Bump it only when an existing name
#: changes meaning; adding names is not a break.
SCHEMA = 1
