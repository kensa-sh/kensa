# Kensa Docs

Kensa's documentation source, authored as MDX and served by [Mintlify](https://mintlify.com).

Mintlify deploys this directory from the core `kensa-sh/kensa` repository. The marketing site
surfaces the deployment at `kensa.sh/docs` through its `/docs` proxy.

## Local preview

Use an LTS Node release (`20`, `22`, or `24`).

```bash
# Install the Mintlify CLI once
npm i -g mint@4.2.693

# From this directory
mint dev
```

The dev server reads `docs.json` for navigation and theme configuration, and picks up the `.mdx`
files in this folder automatically.

## Validation

Run these before shipping larger docs changes:

```bash
mint validate
mint broken-links
mint a11y
```

## Structure

```
docs/
  docs.json            # navigation, theme, colors, SEO
  style.css            # site-wide Mintlify chrome overrides
  introduction.mdx     # landing page of the docs (/)
  quickstart.mdx
  concepts.mdx
  cases.mdx
  assertions.mdx
  judge.mdx
  tracing.mdx
  pytest.mdx
  skills.mdx
  cli.mdx
  ci.mdx
  changelog.mdx
  logo/                # wordmark (light/dark)
  favicon.png
```
