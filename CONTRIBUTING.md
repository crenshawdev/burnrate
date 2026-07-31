# Contributing

Before you write any code, read this. It is short, and it will save us both some hours.

## The short version

I maintain this project by myself. I decide what goes in and what stays out, and the default answer is no. If you can make a compelling case for adding to it, I will hear you out and I might say yes. If I say no, the code is still yours under the license, and you are free to fork it and take it wherever you want. No hard feelings either direction.

That is the whole deal. The rest of this is just me being honest about why, and about how to reach me without wasting your time or mine.

## Why the default is no

I came up on the security side of this work, payment rails that move real money, defense systems, the kind of code where a mistake does not come back as a one-star review, it comes back as somebody's money in the wrong account. That work leaves a mark on how you think. I read every system in front of me as attack surface first, and I have never once regretted it. Every line of code is a thing that can break, a thing somebody can turn against you, a thing I will be maintaining long after the fun of writing it wore off. The feature that does not exist is the one I never have to secure, never have to test against some combination I did not think of, never have to patch when it turns out to do something I never intended. Less code is just safer. That is the belief this whole project sits on, and saying no is how I keep it true.

## What a compelling case looks like

I am not immovable. I have merged plenty of things that started as somebody else's idea. What moves me is not polish, and it is not how much work you already put in, because I never asked you to. What moves me is one honest question, would I use this myself.

If the answer is yes, if it solves a real problem I actually have and not a hypothetical one, and it cannot live just as well in your own fork or a plugin, and I would reach for it in my own daily work, then it has earned a look. When something clears that bar I become its maintainer, gladly, because by then it is a thing I want in my own hands.

If the answer is no, that is not a verdict on you. It just means the thing belongs in your tree and not mine. Fork it and make it exactly what you need.

## How to actually contribute

Talk to me first. This part is not optional. Before you write a line meant to come back into my tree, open an issue and pitch it. Most ideas resolve right there in the conversation, one way or the other, and that conversation is the thing that saves you from pouring a weekend into a pull request I was always going to close. A PR that shows up cold, with no prior discussion and no issue behind it, gets closed on sight. Not out of rudeness. I just will not review unsolicited scope, and I would rather tell you that up front than burn the hours we would both spend.

There is one exception, and it is a good one. If I have already blessed the work, the conversation already happened. If you go digging through the open issues and find a bug I have accepted or a change I have signed off on, that approval is your green light. You do not need to ask me again. Follow the access and signing rules below, open the PR against that issue, and I will review it and tell you what I think.

Forking needs no permission at all, ever. The license hands you that outright. You can fork this the second you land on it and do whatever you please on your own copy, and you never owe me a word about it. Talk-first only kicks in when you want your code to come back into mine.

## Where the code lives

I host this myself at [git.jcrenshaw.dev](https://git.jcrenshaw.dev/crenshawdev/burnrate), and that is the copy that matters. The GitHub mirror exists so people who live on GitHub can find the project and clone it, and I push to it, but the forge is where the issues and the pull requests belong. Open yours there.

## Access and signing

Reads are open to the world, so anyone can browse and clone without an account. To push, you need one, and I do not run open registration, because an account a spam bot can mint on its own is an account I have to babysit. Getting one is easy. Say hello, tell me you want to contribute, and I will approve you by hand, usually fast.

Two rules, no exceptions. Access is over SSH. And every single commit is GPG signed. Not most of them, not just the important ones, every one, and unsigned work does not get in. This is not me being difficult for sport. It is the exact standard I hold my own commits to, and I am not going to ask less of the code carrying this project's name than I ask of myself.

`main` is protected and takes no direct pushes, mine included. Everything lands through a pull request, so branch your work, sign it, and open it against `main`.

## What this thing is, and what it has to stay

burnrate is one Python file and the standard library. No dependencies, no build step, no package to install, and the whole install story is a plugin. `zstandard` is the single outside import anywhere in it, it is optional, and when it is missing the tool still runs against your live transcripts and says so in the report instead of failing. That is not an accident I am willing to trade for convenience. The promise of the thing is that you point it at your own transcripts on whatever machine has Python on it and it works, and every dependency I take on is one more thing standing between you and that.

Two constraints fall out of it, and a patch that breaks either one is not getting in. `burnrate.py` stays a single standalone file that runs on its own, no sibling imports, no package layout, no helper module it cannot live without. And the skill stays a wrapper, never a fork. It calls the `burnrate.py` that shipped beside it and never carries its own copy of the arithmetic, because the moment there are two copies of the math one of them is wrong and neither of us knows which.

The report is the same deal. It is one self-contained HTML file with no network calls in it, it opens offline, and it stays that way. A chart library loaded from a CDN is a request going out from a page built from somebody's private transcripts, and that is not a tradeoff I am interested in hearing a case for.

## Before you open the PR

Run the suite.

```sh
python3 tools/selftest.py
```

Stdlib only, like everything else. No pytest, no fixture tree to install, no network. It has to come back OK on your machine before I will look at your diff.

If your change is a behavior change, it needs a case of its own, and that case has to fail without your patch and pass with it. I am not asking for coverage theater. I am asking for the one test that proves the thing you say you fixed was actually broken, because a test written after the fact against code that already works proves nothing except that you can write a test.

## Forking is not the consolation prize

I want to be plain about this, because I have stood on the other side of a gate that treated forking like failure. If I decline your change, you have lost nothing. You are holding a complete copy of the code and every right to take it your own direction, build the thing I would not, and ship it under your name. That is the design working exactly as intended, not the design breaking down.

I say no to keep mine small. You say yes to build yours. Same freedom, pointed two different ways. I did this myself once, walked away from somebody else's store and stood up my own rather than live under a gate I did not trust, and it was the best call I made that year. If your vision and mine part ways, the door out is right there, and I mean that as respect, not a brush-off.

## Things I will almost never take

This is the short list of what I turn down by reflex, so read it and rule yourself out before you open an issue if you need to.

New third-party dependencies. Every one is somebody else's code, somebody else's bugs, and somebody else's attack surface bolted onto mine, and I will fight hard to avoid adding one.

New config knobs and options. Each switch is another combination I now have to test and defend forever, and they breed faster than anyone admits.

Mission creep. This tool does one clear thing, it reads transcripts you already have and shows you what they cost. A proposal that stretches it into a second or a third thing is a proposal for a different tool, and that tool should be your fork.

Anything that phones home. No telemetry, no update check, no analytics, not behind a flag, not opt-in, not "just once on first run." The tool reads local files and writes local files, and that sentence in the README is a promise I intend to keep being able to make.

Stylistic rewrites. Reformatting, renaming, re-architecting to somebody's personal taste. If it works and it is clear, I am not going to churn it because the braces would sit differently in your editor.

Duplicating what a fork or a plugin should own. If the thing can live outside the core just as well, that is where it belongs.

None of these are iron law, and I have broken every one of them for a good enough reason. They are just where the burden of proof gets steep.

## On AI

I do not care whether you used AI to write your contribution. I use it heavily myself, it drafts the large majority of my code, and then I read every line before my name goes anywhere near it. That last part is the whole thing.

The line was never AI or no-AI, and a policy built on that line measures the wrong variable and lands on the wrong people. What I care about is whether you understand what you are handing me. Can you read it, can you defend every line, can you find the flaw when it is your own, and will you still be here to answer for it when it breaks at three in the morning. Bring me code that clears that bar and I could not care less what wrote the first draft. Bring me something you cannot explain, machine-written or hand-written, and we are done, because the danger was never the tool, it was shipping what you cannot read.

I will review your pull request the exact way I review my own, line by line, as if you typed every character by hand. That is the courtesy I extend my own work, and it is the one I expect you to have already extended yours.

If you want the long version of how I think about this, I have written it out.

- [AI Is Not the Enemy. It's Not the Savior Either.](https://jcrenshaw.dev/posts/ai-is-not-the-enemy-its-not-the-savior-either)
- [The Gap Between Looking Finished and Being Finished](https://jcrenshaw.dev/posts/the-gap-between-looking-finished-and-being-finished)
- [The Gap Runs Both Ways](https://jcrenshaw.dev/posts/the-gap-runs-both-ways)

## Last thing

None of this is meant to scare you off. If you are still reading, you are probably the kind of person I want sending me code. Come say hello, bring me something you understand and can stand behind, and let us build something small and solid. And if we do not end up agreeing, fork it and go build yours. Either way, welcome.
