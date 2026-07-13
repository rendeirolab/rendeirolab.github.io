---
title: "Sharing papers, the lab way"
subtitle: "From a lab email thread to a public page with RSS"
date: 2026-06-07
---

In our lab, as in many others we share a lot of papers. Some are just filled in *to read later*, some get only a quick scan of abstract and figures, some deeply inspected. In many cases these are shared among the lab with a note on the relevance.

The backbone of this system came from my PhD time: back in Christoph Bock's lab, there was a simple mailing list where anyone could post interesting papers. It wasn't fancy, but it worked nicely as an archive. You could search it but mostly it was used as a barometer for what people were excited about at any given time. That system stuck with me. From day one of starting my own group, I set up the same thing: an email address where we send papers with `[paper]` in the subject, a comment, and a link.

For the longest time, the list was just that - a mailbox. Useful to us, invisible to everyone else.

Last week I finally got around to changing that. I wrote a script that pulled all 554 papers from the inbox, extracts titles and DOIs from the URLs, looks up the journal names via Crossref, and renders them into a proper page on our lab site. It also generates an RSS feed, because I'm still nostalgic for the late 2000s internet and think feeds deserve a comeback. Finally, a UMAP and a few other plots describe the data - what is trending with time, how many and when papers are shared. Yes, the data scientist in me will never die.

It pairs nicely with the little [browser extension](https://github.com/rendeirolab/sendpaper-chrome-extension) we built a while back - one click to compose a `[paper]` email with the title and URL pre-filled.

You can see the result at [rendeiro.group/cool-papers/](/cool-papers/#insights) and subscribe to the [RSS feed](/cool-papers/feed.xml). If you build something similar in your lab, I'd love to hear about it.
