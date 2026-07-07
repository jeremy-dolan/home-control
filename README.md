# home-control

A unified interface to control *every* device in your home. *ALL* smart devices
are supported! Whatever hardware you've got, just ask Claude to wire it up :)

<p align="center">
  <img src="assets/demo.gif" width="746" />
</p>

<!-- Or, 7,000 lines of Python hallucinated by Claude that let me turn my
lights off. -->

This is the first app I've vibe-coded from start to finish without any
intervention. As an experiment, I didn't allow myself to look at the code at
all. Claude Opus 4.8 with --dangerously-skip-permissions was given free reign
of a sandbox, with some impressive results (though God knows what's under the
hood):

- Opus one-shot the voice-command mode even without access to an API key for
  testing.
- Opus also managed to reverse engineer the heavily obfuscated login process
  for my router (which I had failed to do a year prior, even with ChatGPT's
  help).

I did start Claude off with copies of my (already working) standalone TUIs for
my lights and Roku, but that was the last I considered the architecture.
