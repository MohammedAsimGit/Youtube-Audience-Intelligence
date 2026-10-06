"""Exact-parity fast path for VADER scoring (Sprint 5.2 NLP optimization).

WHY THIS EXISTS (measured, backend/benchmarks/bench_pipeline.py micro/cProfile):
VADER (`vaderSentiment`) is ~74% of the analysis stage. Profiling showed the
algorithm's cost is NOT the lexicon lookups but repeated *recomputation of
the same lowered word list*:

  - ``_negation_check`` rebuilds ``[str(w).lower() for w in words]`` on EVERY
    call (~3 calls per lexicon word);
  - ``_special_idioms_check`` rebuilds it again per call (plus 5 string
    formats over it);
  - ``_but_check`` rebuilds it once per comment;
  - ``sentiment_valence``/main-loop re-``.lower()`` the same tokens inline.

Together that was ~663k ``str.lower()`` calls per 2,000 comments.

WHAT THIS MODULE CHANGES: nothing about the algorithm. ``polarity_scores``
computes the lowered word list ONCE per comment and threads it through
verbatim copies of the base-class steps. The emoji-conversion loop (a
per-character scan + string rebuild even when there is no emoji) is skipped
via a precomputed frozenset membership test when the text contains no emoji
character - producing the exact same string the base loop would.

WHAT IS DELIBERATELY UNCHANGED: the lexicon, booster/negator tables,
special-case idioms, scalar damping, cap-differential, punctuation
emphasis, ``normalize()``/rounding - all inherited from the base class, so
every score is bit-for-bit identical (verified by tests/test_vader_fast.py
parity checks over the benchmark corpus and adversarial idioms).

Fidelity contract: each ``_fast`` helper is a line-for-line copy of its base
counterpart with ONLY ``words_and_emoticons_lower`` hoisted into the passed
``lowered`` argument. If vaderSentiment changes these methods on upgrade,
the parity test fails loudly (requirements pin + test = the guard).

Thread safety: same as the base class - all state is per-call (lowered is a
local argument, never instance state), so one analyzer instance is safely
shared. Production inference is additionally serialized under
SentimentService's run lock.
"""
from typing import List

from vaderSentiment.vaderSentiment import (
    BOOSTER_DICT,
    C_INCR,
    N_SCALAR,
    SPECIAL_CASES,
    SentimentIntensityAnalyzer,
    SentiText,
    negated,
    scalar_inc_dec,
)


class FastSentimentIntensityAnalyzer(SentimentIntensityAnalyzer):
    """Drop-in ``SentimentIntensityAnalyzer`` with an identical score.

    Same lexicon files, same public API (``polarity_scores``), same results.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Emoji keys are looked up per CHARACTER in the base class; a
        # frozenset lets us skip the whole conversion when there is no
        # candidate character at all.
        self._emoji_chars = frozenset(self.emojis)

    def _convert_emoji(self, text: str) -> str:
        """Verbatim copy of the base-class emoji conversion + strip (used
        only when the text actually contains an emoji character)."""
        text_no_emoji = ""
        prev_space = True
        for chr in text:
            if chr in self.emojis:
                # get the textual description
                description = self.emojis[chr]
                if not prev_space:
                    text_no_emoji += ' '
                text_no_emoji += description
                prev_space = False
            else:
                text_no_emoji += chr
                prev_space = chr == ' '
        return text_no_emoji.strip()

    def polarity_scores(self, text):
        """Same contract/results as the base implementation.

        Differences (all score-preserving):
        - the per-character emoji scan is skipped when no character of
          ``text`` can be an emoji key (frozenset test), otherwise the
          verbatim base loop runs;
        - the lowered token list is computed once and passed down instead
          of being rebuilt inside every helper.
        """
        # --- emoji conversion (base semantics, fast gate) ---------------
        if set(text) & self._emoji_chars:
            text = self._convert_emoji(text)
        else:
            text = text.strip()  # == base: identity rebuild + strip

        sentitext = SentiText(text)
        words_and_emoticons = sentitext.words_and_emoticons
        # Hoisted invariant: identical expression to every base helper's
        # `[str(w).lower() for w in words_and_emoticons]`.
        lowered: List[str] = [str(w).lower() for w in words_and_emoticons]

        sentiments = []
        for i, item in enumerate(words_and_emoticons):
            valence = 0
            # check for vader_lexicon words that may be used as modifiers or negations
            if lowered[i] in BOOSTER_DICT:
                sentiments.append(valence)
                continue
            if (i < len(words_and_emoticons) - 1 and lowered[i] == "kind" and
                    lowered[i + 1] == "of"):
                sentiments.append(valence)
                continue

            sentiments = self._sentiment_valence_fast(
                valence, sentitext, item, i, sentiments, lowered
            )

        sentiments = self._but_check_fast(words_and_emoticons, sentiments, lowered)

        return self.score_valence(sentiments, text)

    # ------------------------------------------------------------------
    # Verbatim copies of the base hot path with `lowered` hoisted.
    # ------------------------------------------------------------------

    def _sentiment_valence_fast(
        self, valence, sentitext, item, i, sentiments, lowered
    ):
        is_cap_diff = sentitext.is_cap_diff
        words_and_emoticons = sentitext.words_and_emoticons
        item_lowercase = lowered[i]
        if item_lowercase in self.lexicon:
            # get the sentiment valence
            valence = self.lexicon[item_lowercase]

            # check for "no" as negation for an adjacent lexicon item vs
            # "no" as its own stand-alone lexicon item
            if item_lowercase == "no" and i != len(words_and_emoticons) - 1 \
                    and lowered[i + 1] in self.lexicon:
                # don't use valence of "no" as a lexicon item. Instead set
                # it's valence to 0.0 and negate the next item
                valence = 0.0
            if (i > 0 and lowered[i - 1] == "no") \
               or (i > 1 and lowered[i - 2] == "no") \
               or (i > 2 and lowered[i - 3] == "no" and lowered[i - 1] in ["or", "nor"]):
                valence = self.lexicon[item_lowercase] * N_SCALAR

            # check if sentiment laden word is in ALL CAPS (while others aren't)
            if item.isupper() and is_cap_diff:
                if valence > 0:
                    valence += C_INCR
                else:
                    valence -= C_INCR

            for start_i in range(0, 3):
                # dampen the scalar modifier of preceding words and emoticons
                # (excluding the ones that immediately preceed the item) based
                # on their distance from the current item.
                if i > start_i and lowered[i - (start_i + 1)] not in self.lexicon:
                    # NOTE: scalar_inc_dec still receives the ORIGINAL token
                    # because it checks word.isupper() (lowering would lose
                    # the case signal) - identical to base.
                    s = scalar_inc_dec(
                        words_and_emoticons[i - (start_i + 1)], valence, is_cap_diff
                    )
                    if start_i == 1 and s != 0:
                        s = s * 0.95
                    if start_i == 2 and s != 0:
                        s = s * 0.9
                    valence = valence + s
                    valence = self._negation_check_fast(
                        valence, words_and_emoticons, lowered, start_i, i
                    )
                    if start_i == 2:
                        valence = self._special_idioms_check_fast(
                            valence, words_and_emoticons, lowered, i
                        )

            valence = self._least_check_fast(valence, words_and_emoticons, lowered, i)
        sentiments.append(valence)
        return sentiments

    @staticmethod
    def _negation_check_fast(valence, words_and_emoticons, lowered, start_i, i):
        # Base copy: the ONLY change is receiving `lowered` instead of
        # rebuilding [str(w).lower() for w in words_and_emoticons] per call.
        words_and_emoticons_lower = lowered
        if start_i == 0:
            if negated([words_and_emoticons_lower[i - (start_i + 1)]]):  # 1 word preceding lexicon word (w/o stopwords)
                valence = valence * N_SCALAR
        if start_i == 1:
            if words_and_emoticons_lower[i - 2] == "never" and \
                    (words_and_emoticons_lower[i - 1] == "so" or
                     words_and_emoticons_lower[i - 1] == "this"):
                valence = valence * 1.25
            elif words_and_emoticons_lower[i - 2] == "without" and \
                    words_and_emoticons_lower[i - 1] == "doubt":
                valence = valence
            elif negated([words_and_emoticons_lower[i - (start_i + 1)]]):  # 2 words preceding lexicon word position
                valence = valence * N_SCALAR
        if start_i == 2:
            if words_and_emoticons_lower[i - 3] == "never" and \
                    (words_and_emoticons_lower[i - 2] == "so" or words_and_emoticons_lower[i - 2] == "this") or \
                    (words_and_emoticons_lower[i - 1] == "so" or words_and_emoticons_lower[i - 1] == "this"):
                valence = valence * 1.25
            elif words_and_emoticons_lower[i - 3] == "without" and \
                    (words_and_emoticons_lower[i - 2] == "doubt" or words_and_emoticons_lower[i - 1] == "doubt"):
                valence = valence
            elif negated([words_and_emoticons_lower[i - (start_i + 1)]]):  # 3 words preceding lexicon word position
                valence = valence * N_SCALAR
        return valence

    @staticmethod
    def _special_idioms_check_fast(valence, words_and_emoticons, lowered, i):
        # Base copy with the per-call lowered rebuild hoisted.
        words_and_emoticons_lower = lowered
        onezero = "{0} {1}".format(words_and_emoticons_lower[i - 1], words_and_emoticons_lower[i])

        twoonezero = "{0} {1} {2}".format(words_and_emoticons_lower[i - 2],
                                          words_and_emoticons_lower[i - 1], words_and_emoticons_lower[i])

        twoone = "{0} {1}".format(words_and_emoticons_lower[i - 2], words_and_emoticons_lower[i - 1])

        threetwoone = "{0} {1} {2}".format(words_and_emoticons_lower[i - 3],
                                           words_and_emoticons_lower[i - 2], words_and_emoticons_lower[i - 1])

        threetwo = "{0} {1}".format(words_and_emoticons_lower[i - 3], words_and_emoticons_lower[i - 2])

        sequences = [onezero, twoonezero, twoone, threetwoone, threetwo]

        for seq in sequences:
            if seq in SPECIAL_CASES:
                valence = SPECIAL_CASES[seq]
                break

        if len(words_and_emoticons_lower) - 1 > i:
            zeroone = "{0} {1}".format(words_and_emoticons_lower[i], words_and_emoticons_lower[i + 1])
            if zeroone in SPECIAL_CASES:
                valence = SPECIAL_CASES[zeroone]
        if len(words_and_emoticons_lower) - 1 > i + 1:
            zeroonetwo = "{0} {1} {2}".format(words_and_emoticons_lower[i], words_and_emoticons_lower[i + 1],
                                              words_and_emoticons_lower[i + 2])
            if zeroonetwo in SPECIAL_CASES:
                valence = SPECIAL_CASES[zeroonetwo]

        # check for booster/dampener bi-grams such as 'sort of' or 'kind of'
        n_grams = [threetwoone, threetwo, twoone]
        for n_gram in n_grams:
            if n_gram in BOOSTER_DICT:
                valence = valence + BOOSTER_DICT[n_gram]
        return valence

    def _least_check_fast(self, valence, words_and_emoticons, lowered, i):
        # Base copy with the per-call lowered rebuild hoisted.
        if i > 1 and lowered[i - 1] not in self.lexicon \
                and lowered[i - 1] == "least":
            if lowered[i - 2] != "at" and lowered[i - 2] != "very":
                valence = valence * N_SCALAR
        elif i > 0 and lowered[i - 1] not in self.lexicon \
                and lowered[i - 1] == "least":
            valence = valence * N_SCALAR
        return valence

    @staticmethod
    def _but_check_fast(words_and_emoticons, sentiments, lowered):
        # Base copy with the per-call lowered rebuild hoisted.
        words_and_emoticons_lower = lowered
        if 'but' in words_and_emoticons_lower:
            bi = words_and_emoticons_lower.index('but')
            for sentiment in sentiments:
                si = sentiments.index(sentiment)
                if si < bi:
                    sentiments.pop(si)
                    sentiments.insert(si, sentiment * 0.5)
                elif si > bi:
                    sentiments.pop(si)
                    sentiments.insert(si, sentiment * 1.5)
        return sentiments
