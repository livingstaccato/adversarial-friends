# Image Assets

Canonical home for visual assets used by `README.md` and distributed skill
docs.

```
docs/images/
└── brand/
    ├── afriend-banner.png      (1024×1024, branded mark)
    ├── afriend-logo-128.png    (derived size)
    ├── afriend-logo-256.png
    └── afriend-logo-512.png
```

The banner is the source of truth; the numbered sizes are derived from it with
`sips -z <n> <n>` and are regenerated rather than edited. The 2048×2048
original is deliberately not committed — a full-resolution PNG of this
illustration runs to several megabytes and the banner is only ever rendered at
README width.

**Every image reference in `README.md` uses an absolute
`https://raw.githubusercontent.com/...` URL, never a relative path.** A
relative path resolves only when the README is rendered inside the repository
tree — it breaks on PyPI, in package registries, and anywhere the README is
mirrored or embedded. `test_readme_image_links_are_absolute_github_urls`
enforces this.

## Regenerating

```bash
for size in 128 256 512; do
  sips -s format png -z "$size" "$size" \
       docs/images/brand/afriend-banner.png \
       --out "docs/images/brand/afriend-logo-${size}.png"
done
```
