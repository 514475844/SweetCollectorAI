"""Media Resource Manager.

A local, compliance-first tool for discovering media across multi-level
directory structures on sites the user is *legally authorized* to access,
then managing the local library (dedup, classify, review, export).

Design rules baked into the codebase:
  * Never bypass login / paywall / DRM / captcha / geo / access controls.
  * No built-in site rules. The user supplies every adapter.
  * Respect robots.txt and the site's terms before any fetch.
  * Local-only AI: no network calls for classification or preference learning.
  * Destructive operations are undoable (isolation dir / recycle bin) by default.
"""

VERSION = "0.0.67"
__version__ = VERSION
