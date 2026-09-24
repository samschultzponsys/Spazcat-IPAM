# Fonts

Font files in this folder get **baked into the Docker image** and show up in
**Settings → Appearance** as choices for the logo, the headers and the body text.

## Add a font (via GitHub)

1. In GitHub, open this folder (`app/static/fonts/`) → **Add file → Upload files**.
2. Drop in `.otf`, `.ttf`, `.woff` or `.woff2` files and commit to `main`.
3. The **build-image** Action rebuilds the image with the fonts inside.
4. Pull the new image (`docker compose pull && docker compose up -d`), open
   Settings → Appearance, and pick your fonts.

You don't need to edit any HTML or CSS. The server reads this folder and generates
the `@font-face` rules itself (`/api/fonts.css`).

## Naming

The file name becomes the name in the dropdown: `Big_Shoulders-Bold.ttf` shows
up as **Big Shoulders Bold**. Each file is its own option, so upload the exact
weight or style you want (e.g. the Bold file for a heavy logo).

## Default logo font

If a file with `ethnocentric` in its name is here (e.g. `ethnocentric rg.otf`),
the logo uses it by default. That keeps the original Spazcat wordmark. Without
one, everything defaults to the monospace stack.

## Without rebuilding

To try a font without a rebuild, drop it into `data/fonts/` on the host (next
to `ipam.db`) and reload the page. A file there overrides one with the same name
baked into the image.

## Licensing

Only commit fonts whose license allows redistribution. The image is public on
GHCR, and so are the fonts inside it.
