# assets

Images for the README live here (screenshots, exported diagrams).

The architecture diagram's source of truth is
[`../diagrams/architecture.mmd`](../diagrams/architecture.mmd). GitHub renders
it directly in the main README. To export a PNG copy:

```bash
npx -y @mermaid-js/mermaid-cli -i diagrams/architecture.mmd -o assets/architecture.png
```
