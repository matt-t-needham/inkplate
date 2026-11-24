Fonts vendored into this project (both are redistributable; kept in-repo so
the Docker image needs no network at build time).

Michroma-Regular.ttf
  Michroma — © Vernon Adams. SIL Open Font License 1.1 (via Google Fonts).
  The same display font the rage channel burns into its captions; copied from
  rageagain_channel/fonts/. Used for temperatures, day names, now-playing.
  Note: its own degree glyph renders as a clunky baseline "o" — screens._deg()
  draws degree marks as rings instead.

weathericons-regular-webfont.ttf
  Weather Icons — © Erik Flowers, icons by Lukas Bischoff.
  SIL Open Font License 1.1. https://erikflowers.github.io/weather-icons/
  Fetched from https://github.com/erikflowers/weather-icons (font/).
  Used for the weather condition icons and the sunrise/sunset glyphs.
  Codepoints live in screens.WI_GLYPHS — they are Private Use Area chars, so
  reference them as chr(0xf00d) rather than pasting literals (literal PUA
  characters do not survive every editor/pipe).

Caveat-Variable.ttf  (+ Caveat-OFL.txt)
  Caveat — © 2014 The Caveat Project Authors. SIL Open Font License 1.1.
  Fetched from https://github.com/google/fonts (ofl/caveat/Caveat[wght].ttf,
  renamed). A variable font with one weight axis (400-700); its only named
  instances are Regular and Bold, so screens._hand sets the weight by axis
  (600 for the place, 500 for the date). The marker hand on the photo
  polaroid's foot (DECISIONS §32).
