# Third-party notices

This project includes ideas or code adapted from the third-party software listed
below. Each entry names what was used, where it lives in this repository, and the
licence it is used under.

## munder-difflin

- Source: https://github.com/chaitanyagiri/munder-difflin
- Copyright (c) 2026 Chaitanya Giri
- Licence: MIT
- Used in: `ui/dashboard/static/office.js` (the Mission Control "Office" tab)
- What was used: scene techniques from its office floor
  (`src/renderer/src/scene/office/`), re-implemented from scratch on a plain
  2D canvas. These are avatars seated at desks that animate while their agent
  works, a desk device that lights up while its owner types, a thought cloud with
  trailing puffs above a busy avatar, a red "!" over a blocked avatar,
  papers and envelopes that fly along an eased arc with an arrival burst, and a
  stable first-free seat pool.
- Not used: none of its source files, art or bundled assets were copied. Its
  `src/renderer/src/assets/` tilesets are third-party and not redistributable, so
  every sprite in `office.js` is drawn procedurally from original palettes. No
  characters, names or likenesses from any TV show are reproduced.

MIT License

Copyright (c) 2026 Chaitanya Giri

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
