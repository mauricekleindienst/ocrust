# Publishing these pages as the GitHub wiki

The files in this directory *are* the wiki: page names, `[[Links]]` and the
`Home` entry point all follow GitHub's wiki conventions. They live in the
repository so they are reviewed and versioned with the code.

To publish them:

1. Enable the wiki once: **Settings → Features → Wikis**, then create any page
   in the browser so GitHub initializes the wiki repository.
2. Push these files into it:

   ```bash
   git clone https://github.com/mauricekleindienst/ocrust.wiki.git /tmp/ocrust-wiki
   cp wiki/*.md /tmp/ocrust-wiki/
   rm -f /tmp/ocrust-wiki/_publish.md
   cd /tmp/ocrust-wiki
   git add -A && git commit -m "docs: sync wiki from the repository"
   git push
   ```

`Home.md` is the landing page. `[[Python API]]` resolves to `Python-API.md`,
`[[PDF workflows]]` to `PDF-workflows.md`, and so on — GitHub maps spaces to
hyphens, so the names must keep their current spelling.
