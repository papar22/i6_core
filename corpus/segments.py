__all__ = [
    "SegmentCorpus",
    "SegmentCorpusBySpeaker",
    "SegmentCorpusByRegex",
    "ShuffleAndSplitSegments",
    "SplitSegmentFile",
    "SortSegmentsByLengthAndShuffle",
    "UpdateSegmentsWithSegmentMap",
]

import collections
import itertools as it
import os
import random
import re

import numpy as np

from recipe.i6_asr.util import MultiOutputPath
from recipe.i6_asr.lib import corpus
from recipe.i6_asr.util import chunks

from sisyphus import *

Path = setup_path(__package__)


class SegmentCorpus(Job):
    def __init__(self, corpus_path, num_segments):
        self.set_vis_name("Segment Corpus")

        self.corpus_path = corpus_path
        self.num_segments = num_segments
        self.single_segment_files = dict(
            (i, self.output_path("segments.%d" % i)) for i in range(1, num_segments + 1)
        )
        self.segment_path = MultiOutputPath(
            self, "segments.$(TASK)", self.single_segment_files
        )

    def tasks(self):
        yield Task("run", resume="run", mini_task=True)

    def run(self):
        c = corpus.Corpus()
        c.load(tk.uncached_path(self.corpus_path))

        all_segments = list(c.segments())

        for idx, segments in enumerate(chunks(all_segments, self.num_segments)):
            with open(
                self.single_segment_files[idx + 1].get_path(), "wt"
            ) as segment_file:
                for segment in segments:
                    segment_file.write(segment.fullname() + "\n")


class SegmentCorpusBySpeaker(Job):
    def __init__(self, corpus_path, num_speakers=None):
        self.set_vis_name("Segment By Speaker")

        self.corpus_path = corpus_path

        self.num_speakers = self.output_var("num_speakers", True)
        self.segment_dir = self.output_path("segments", True)
        self.segment_path = MultiOutputPath(
            self, "segments/speaker.$(TASK)", self.segment_dir
        )
        self.speaker_map_file = self.output_path("speaker.map")
        self.cluster_map_file = self.output_path("cluster.map.xml")

    def tasks(self):
        yield Task("run", resume="run", mini_task=True)

    def run(self):
        c = corpus.Corpus()
        c.load(tk.uncached_path(self.corpus_path))
        speaker_map = collections.defaultdict(list)

        for segment in c.segments():
            speaker = segment.speaker()
            speaker = "unknown" if speaker is None else speaker.name
            speaker_map[speaker].append(segment.fullname())

        self.num_speakers.set(len(speaker_map))

        with open(self.speaker_map_file.get_path(), "wt") as smf:
            with open(self.cluster_map_file.get_path(), "wt") as cmf:
                cmf.write('<?xml version="1.0" encoding="utf-8" ?>\n')
                cmf.write("<coprus-key-map>\n")  # misspelled on purpose
                for idx, speaker in enumerate(sorted(speaker_map), 1):
                    smf.write("%s\n" % speaker)
                    with open(
                        os.path.join(self.segment_dir.get_path(), "speaker.%d" % idx),
                        "wt",
                    ) as ssf:
                        for segment in speaker_map[speaker]:
                            ssf.write("%s\n" % segment)
                            cmf.write(
                                '  <map-item key="%s" value="cluster.%d"/>\n'
                                % (segment, idx)
                            )
                cmf.write("</coprus-key-map>")  # misspelled on purpose


class SegmentCorpusByRegex(Job):
    def __init__(
        self, corpus_path, regex, regex_flags=0, use_fullpath=False, groups=None
    ):
        self.set_vis_name("Segment By Regex")

        self.corpus_path = corpus_path
        self.regex = re.compile(regex, regex_flags)
        self.use_fullpath = use_fullpath
        self.groups = groups if groups is not None else [1]

        self.num_speakers = self.output_var("num_speakers", True)
        self.segment_dir = self.output_path("segments", True)
        self.segment_path = MultiOutputPath(
            self, "segments/speaker.$(TASK)", self.segment_dir
        )
        self.speaker_map_file = self.output_path("speaker.map")
        self.cluster_map_file = self.output_path("cluster.map.xml")

    def tasks(self):
        yield Task("run", mini_task=True)

    def run(self):
        c = corpus.Corpus()
        c.load(tk.uncached_path(self.corpus_path))
        speaker_map = {}
        for segment in c.segments():
            if self.use_fullpath:
                match = self.regex.search(segment.fullname())
            else:
                match = self.regex.search(segment.name)
            if match is not None:
                if len(match.groups()) > 0:
                    speaker = ""
                    for g in self.groups:
                        if match.group(g) is not None:
                            speaker += match.group(g)
                else:
                    speaker = match.group(0)
            else:
                speaker = "unknown"

            if speaker not in speaker_map.keys():
                speaker_map[speaker] = []

            speaker_map[speaker].append(segment.fullname())

        self.num_speakers.set(len(speaker_map))

        with open(self.speaker_map_file.get_path(), "wt") as smf:
            with open(self.cluster_map_file.get_path(), "wt") as cmf:
                cmf.write('<?xml version="1.0" encoding="utf-8" ?>\n')
                cmf.write("<coprus-key-map>\n")  # misspelled on purpose
                for idx, speaker in enumerate(sorted(speaker_map), 1):
                    smf.write("%s\n" % speaker)
                    with open(
                        os.path.join(self.segment_dir.get_path(), "speaker.%d" % idx),
                        "wt",
                    ) as ssf:
                        for segment in speaker_map[speaker]:
                            ssf.write("%s\n" % segment)
                            cmf.write(
                                '  <map-item key="%s" value="cluster.%d"/>\n'
                                % (segment, idx)
                            )
                cmf.write("</coprus-key-map>")  # misspelled on purpose


class ShuffleAndSplitSegments(Job):
    default_split = {"train": 0.9, "dev": 0.1}

    def __init__(
        self, segment_file, split=None, shuffle=True, shuffle_seed=0x3C5EA3E47D4E0077
    ):
        if split is None:
            split = dict(**self.default_split)

        assert isinstance(split, dict)
        assert all(s > 0 for s in split.values())
        assert abs(sum(split.values()) - 1.0) < 1e-10

        self.segment_file = segment_file
        self.split = split
        self.shuffle = shuffle
        self.shuffle_seed = shuffle_seed

        self.new_segments = {
            k: self.output_path("%s.segments" % k) for k in self.split.keys()
        }

    def tasks(self):
        yield Task("run", mini_task=True)

    def run(self):
        with open(tk.uncached_path(self.segment_file)) as f:
            segments = f.readlines()

        if self.shuffle:
            rng = random.Random(self.shuffle_seed)
            rng.shuffle(segments)

        ordered_keys = sorted(self.split.keys())
        n = len(segments)
        split_idx = [0] + [
            int(n * c) for c in it.accumulate(self.split[k] for k in ordered_keys)
        ]
        split_idx[
            -1
        ] = n  # just in case we get numeric errors that drop the last element

        for i, k in enumerate(ordered_keys):
            with open(self.new_segments[k].get_path(), "wt") as f:
                f.writelines(segments[split_idx[i] : split_idx[i + 1]])

    @classmethod
    def hash(cls, kwargs):
        kwargs_copy = dict(**kwargs)
        if kwargs_copy["split"] is not None:
            split = kwargs_copy["split"]
            if len(split) == len(cls.default_split) and all(
                k in cls.default_split and cls.default_split[k] == v
                for k, v in split.items()
            ):
                kwargs_copy["split"] = None

        return super().hash(kwargs_copy)


class SplitSegmentFile(Job):
    def __init__(self, segment_file, concurrent=1):
        self.segment_file = segment_file
        self.concurrent = concurrent

        self.single_segments = {
            i: self.output_path("segments.%d" % i)
            for i in range(1, self.concurrent + 1)
        }
        self.segment_path = MultiOutputPath(
            self, "segments.$(TASK)", self.single_segments, cached=True
        )

    def tasks(self):
        yield Task("run", resume="run", mini_task=True)

    def run(self):
        with open(tk.uncached_path(self.segment_file), "rt") as f:
            lines = [l for l in f.readlines() if len(l.strip()) > 0]

        n = len(lines)
        m = n % self.concurrent
        end = 0
        for i in range(1, self.concurrent + 1):
            start = end
            end += n // self.concurrent + (1 if i <= m else 0)
            with open(self.single_segments[i].get_path(), "wt") as f:
                f.writelines(lines[start:end])


class SortSegmentsByLengthAndShuffle(Job):
    def __init__(self, crp, shuffle_strength=1.0, shuffle_seed=0x3C5EA3E47D4E0077):
        """
        :param crp: recipe.rasr.crp.CommonRasrParameters
        :param shuffle_strength: float in [0,inf) determines how much the length should affect sorting
                                 0 -> completely random; inf -> strictly sorted
        :param shuffle_seed: random number seed
        """
        self.crp = crp
        self.shuffle_strength = shuffle_strength
        self.shuffle_seed = shuffle_seed

        self.new_segments = self.output_path("segments")

    def tasks(self):
        yield Task("run", mini_task=True)

    def run(self):
        with open(tk.uncached_path(self.crp.segment_path)) as f:
            segments = f.read().splitlines()

        corpus_path = tk.uncached_path(self.crp.corpus_config.file)
        c = corpus.Corpus()
        c.load(corpus_path)
        print(corpus_path)

        segment_dict = {}
        for segment in c.segments():
            if segment.fullname() in segments:
                if np.isinf(segment.end):
                    if segment.recording.audio[-4:] == ".wav":
                        import wave

                        with wave.open(segment.recording.audio) as afile:
                            segment_dict[segment.fullname() + "\n"] = (
                                afile.getnframes() / afile.getframerate()
                            )
                else:
                    segment_dict[segment.fullname() + "\n"] = (
                        segment.end - segment.start
                    )

        probs = np.exp(
            -self.shuffle_strength * np.fromiter(segment_dict.values(), dtype=float)
        )
        probs /= np.sum(probs)

        np.random.seed(self.shuffle_seed)
        seglist = np.random.choice(
            list(segment_dict.keys()), size=len(probs), replace=False, p=probs
        )

        with open(self.new_segments.get_path(), "wt") as f:
            f.writelines(seglist)


class UpdateSegmentsWithSegmentMap(Job):
    """
    Update a segment file with a segment mapping file (e.g. from corpus compression)

    :param Path segment_file: path to the segment text file (uncompressed)
    :param Path segment_map: path to the segment map (gz or uncompressed)
    """

    def __init__(self, segment_file, segment_map):

        self.segment_file = segment_file
        self.segment_map = segment_map
        self.out = self.output_path("updated.segments")

    def tasks(self):
        yield Task("run", mini_task=True)

    def run(self):

        sm = corpus.SegmentMap()
        sm.load(tk.uncached_path(self.segment_map))

        segment_map_dict = {}
        for map_item in sm.map_entries:
            segment_map_dict[map_item.key] = map_item.value

        with open(tk.uncached_path(self.segment_file), "rt") as in_segments, open(
            tk.uncached_path(self.out), "wt"
        ) as out_segments:
            for in_segment in in_segments:
                out_segment = segment_map_dict[in_segment.strip()].strip()
                out_segments.write(out_segment + "\n")