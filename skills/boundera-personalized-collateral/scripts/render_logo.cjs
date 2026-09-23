// Convert an already verified official SVG; do not redraw or recolor the logo.
const fs = require('node:fs');
const path = require('node:path');
const [input, output, sharpModule] = process.argv.slice(2);
if (!input || !output || !sharpModule) throw new Error('Usage: render_logo.cjs input.svg output.png absolute-sharp-module');
if (fs.existsSync(output)) throw new Error('Output already exists; use a new asset name.');
const sharp = require(path.resolve(sharpModule));
sharp(input, {density: 600}).resize({width: 1200}).png().toBuffer().then(data => {
  fs.writeFileSync(output, data, {flag: 'wx'});
  console.log(JSON.stringify({output, bytes: data.length}));
});
