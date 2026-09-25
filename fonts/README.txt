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
