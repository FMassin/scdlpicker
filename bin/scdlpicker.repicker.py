#!/usr/bin/env seiscomp-python
# -*- coding: utf-8 -*-
###########################################################################
# Copyright (C) GFZ Potsdam                                               #
# All rights reserved.                                                    #
#                                                                         #
# Authors:                                                                #
#     Thomas Bornstein                                                    #
#     Joachim Saul (saul@gfz-potsdam.de)                                  #
#                                                                         #
# GNU Affero General Public License Usage                                 #
# This file may be used under the terms of the GNU Affero                 #
# Public License version 3.0 as published by the Free Software Foundation #
# and appearing in the file LICENSE included in the packaging of this     #
# file. Please review the following information to ensure the GNU Affero  #
# Public License version 3.0 requirements will be met:                    #
# https://www.gnu.org/licenses/agpl-3.0.html.                             #
###########################################################################

import os
import yaml
import time
import logging
import numpy as np
import scipy.signal
from typing import Tuple
import argparse
import obspy

# Here is the place to import other DL models
from seisbench.models import EQTransformer, PhaseNet

MODEL_MAP = {
    'phasenet': PhaseNet,
    'eqtransformer': EQTransformer,
}

LOGFORMAT = "%(levelname)-8s  %(asctime)s %(message)s"
logging.basicConfig(format=LOGFORMAT)
logger = logging.getLogger('origin-repicker')
logger.setLevel(logging.DEBUG)


class EventWorkspaceContainer:

    def __init__(self):
        self.picks = dict()
        self.mlpicks = dict()
        self.waveforms = dict()


Pick = obspy.core.AttribDict


def dotted_nslc(pick):
    return "%s.%s.%s.%s" % (
        pick.networkCode,
        pick.stationCode,
        pick.locationCode,
        pick.channelCode )


class Repicker:
    """A class to hold settings and provide methods for arbitrary seisbench
    compatible P wave pickers.

    Besides using the chosen ML model (by instanciating one of the inherited
    repicker classes) to repick the input stream, it also does preparations
    as collecting adhoc picks, constructing a stream depending on the chosen
    batch size, doing sanity checks, as well as post-processing like writing
    out annotations and picks to be further processed bei seiscomp.

    The Repicker is run in an infinity loop polling for new symbolic links
    inside the sub-directory `spool/`. Those links should point to YAML files
    containing picks which are usually residing in an `events/<EVENT>/`
    sub-folder.

    The refined picks are written into YAML files inside `outgoing/`.

    Args:
        eventRootDir(str): Directory containing event folders, used to
                           store annotations.

        spoolDir(str): Directory to watch for links to yaml files

        test(bool): To test the main functionality, stops before writing
                    outgoing files

        batchSize(int): Repicker will set the size of a batch to this
                        (at maximum)

        device(str): Defines where to run the model - "cpu" or "gpu"
    """

    def __init__(self, model_name=None, dataset="geofon",
                 workingDir=".", eventRootDir="events", spoolDir="spool",
                 test=False, exit=False, batchSize=False, device="cpu",
                 minConfidence=None, annotDir="annot"):

        if not model_name in MODEL_MAP:
            raise ValueError("No such model: "+model_name)

        self.model = MODEL_MAP[model_name].from_pretrained(dataset)
        self.model_name = model_name

        self.test = test
        self.exit = exit
        self.workingDir = workingDir
        self.eventRootDir = eventRootDir
        self.spoolDir = spoolDir
        self.batchSize = batchSize
        self.workspaces = dict()
        self.minConfidence = minConfidence
        self.annotDir = annotDir

        if device == "cpu":
            self.model.cpu()
        elif device == "gpu":
            self.model.cuda()

        self.expected_input_length_sec = \
            self.model.in_samples / self.model.sampling_rate

    def _get_stream_from_picks(self, picks, eventID) \
            -> Tuple[str, obspy.core.stream.Stream, list]:
        """
        For the given picks, read the corresponding streams and return them.

        No streams are returned if at least one component is missing. Since
        not always for all picks all streams will be complete, a list of picks
        for which we have all data is returned.
        """

        # Collect streams from mseed files
        collected_picks = []
        stream = None
        eventRootDir = self.eventRootDir
        for pick in picks:
            pick: Pick
            pickID = pick.publicID
            logger.debug("//// " + pickID)

            nslc = (pick.networkCode, pick.stationCode,
                    pick.locationCode, pick.channelCode[0:2])
            nslc = "%s.%s.%s.%s" % nslc

            # Create an obspy stream from according mseed files

            waveforms_d = os.path.join(eventRootDir, eventID, "waveforms")

            # Look for the Z component
            fileZ = os.path.join(waveforms_d, nslc + "Z" + ".mseed")
            if not os.path.exists(fileZ):
                fileZ = None

            # Look for the N or 1 component
            for component in ["N", "1"]:
                fileN = os.path.join(waveforms_d, nslc + component + '.mseed')
                if os.path.exists(fileN):
                    break
            else:
                fileN = None

            # Look for the E or 2 component
            for component in ["E", "2"]:
                fileE = os.path.join(waveforms_d, nslc + component + '.mseed')
                if os.path.exists(fileE):
                    break
            else:
                fileE = None

            # Check for missing files
            if not fileZ and not fileN and not fileE:
                # no data at all -> no debug message needed
                logger.debug("---- " + pickID)
                logger.debug("---- not enough data -> skipped")
                continue
            if not fileZ or not fileN or not fileE:
                # partly missing data
                logger.debug("---- " + pickID)
                logger.debug("---- missing components -> skipped")
                continue

            # All needed files exist
            logger.debug("++++ " + pickID)

            streamZ, streamE, streamN = None, None, None

            # We try to open all three component files and if we fail on
            # any of these we give up.
            try:
                streamZ = obspy.core.stream.read(fileZ)
                streamN = obspy.core.stream.read(fileN)
                streamE = obspy.core.stream.read(fileE)
            # The following are all real-life exceptions observed in the past
            # and which we tolerate for the time being.
            except (TypeError, ValueError,
                    obspy.io.mseed.InternalMSEEDError,
                    obspy.io.mseed.ObsPyMSEEDFilesizeTooSmallError) as e:
                logger.warning(
                    "Caught " + repr(e) + " while processing pick " + pickID)
                continue
            except Exception as e:
                logger.warning("Unknown exception: " + str(e))

            if None in (streamZ, streamE, streamE):
                logger.warning(
                    f" {nslc}: Didn't find mseed files for all components.")
                continue
            else:
                [ _.merge(method=1, fill_value=0, interpolation_samples=0)
                    for _ in [streamZ, streamE, streamN]]
            if not stream:
                stream = obspy.core.stream.Stream()

            # Check if trace is shorter than needed
            for t in (streamZ, streamE, streamN):
                trace_len = t[0].stats.endtime - t[0].stats.starttime
                if trace_len < self.expected_input_length_sec:
                    logger.warning(
                        f"Trace {nslc} ({t[0].meta.channel}): "
                        "length {trace_len:.2f}s is too short. "
                        "Picker needs {self.expected_input_length_sec:.2f}s.")
                    break
            else:
                stream += streamZ
                stream += streamN
                stream += streamE
                collected_picks.append(pick)
        if len(collected_picks) == 0:
            logger.debug(f"Empty stream for event {eventID}.")

        return eventID, stream, collected_picks

    def _process(self, adhoc_picks, eventID):
        """
        Looks for new picks among the passed adhoc_picks, passes them
        to _ml_predict(), adds the new predictions to the event
        workspace and returns all recently calculated ML picks.
        """

        logger.debug("process %s    %d picks" % (eventID, len(adhoc_picks)))

        if eventID not in self.workspaces:
            self.workspaces[eventID] = EventWorkspaceContainer()
        workspace = self.workspaces[eventID]

        # retrieve additional picks from ep
        new_adhoc_picks = []
        for pick in adhoc_picks:
            pick_id = pick.publicID
            # We need to avoid to repeatedly try to process picks
            # we have already finished processing. So we only process
            # picks that we haven't seen for this event. This is OK
            # because there is no cross-talk between picks. However,
            # in case we analyze picks in the context of other
            # picks, we will have to re-process previously processed
            # picks. Then this filtering will not be appropriate.

            # if pickID not in workspace.mlpicks:
            if pick_id not in workspace.picks:
                workspace.picks[pick_id] = pick
                new_adhoc_picks.append(pick)
        tmp = "%d" % len(new_adhoc_picks) if new_adhoc_picks else "no"
        logger.debug(tmp + " new picks")

        if not new_adhoc_picks:
            return []

        # extra debug output to see if we accidentally process
        # any "new" picks twice
        for pick in new_adhoc_picks:
            logger.debug("NEW PICK %s" % pick.publicID)

        # ++++++++++++ Get Predictions +++++++++++++++++#
        predictions = self._ml_predict(new_adhoc_picks, eventID)

        if not predictions:
            logger.warning("processing returned without result")
            return []

        new_picks = list()

        for pick_id in predictions:
            # In order to accommodate more than one maximum exceeding
            # the threshold, this is now a list of (time, confidence)
            # pairs.
            preds = predictions[pick_id]

            triggering_pick = workspace.picks[pick_id]

            for (ml_time, ml_conf) in preds:
                ml_timestamp = (ml_time.isoformat() + "000000")[:23] + "Z"
                logger.info("PICK   %s" % pick_id)
                logger.info("RESULT %s  c= %.2f" % (ml_timestamp, ml_conf))

                # FIXME: temporary criterion
                # On one hand we want as small a time window as
                # possible, but on the other hand it must be large
                # enough to accommodate large due to wrong source depth.
                # TODO: iteration!
                dt_max = 10
                dt = abs(ml_time - obspy.UTCDateTime(triggering_pick.time))
                if abs(dt) > dt_max:
                    logger.info("SKIPPED dt = %.2f" % dt)
                    continue
                if ml_conf < self.minConfidence:
                    logger.info("SKIPPED conf = %.3f" % ml_conf)
                    continue
                old_pick = workspace.picks[pick_id]
                new_pick = old_pick.copy()
                new_pick.publicID = old_pick.publicID + "/repick"
                new_pick.model = self.model_name
                new_pick.confidence = float("%.3f" % ml_conf)
                new_pick.time = ml_timestamp
                # FIXME: new_pick.time is an isotimestamp without uncertainties

                # The key of the ML pick is the publicID of the
                # original pick in order to make association easier.
                # This will later be relevant for relocation, where
                # we will actually replace existing picks with their
                # ML equivalent.
                workspace.mlpicks[pick_id] = new_pick

                # FIXME: For the time being
                assert new_pick not in new_picks
                new_picks.append(new_pick)

        return new_picks

    def _findSpoolItems(self):
        d = self.spoolDir
        filenames = [i for i in os.listdir(d) if i.endswith(".yaml")]
        items = list()
        for filename in sorted(filenames):
            path = os.path.join(d, filename)

            if os.path.islink(path):
                target = os.readlink(path)
                target = os.path.join(d, target)
                if not os.path.exists(target):
                    logger.warning("missing " + target)
                    continue

                items.append( (path, target) )
        return items

    def _readPicksFromYaml(self, yamlfilename):
        with open(yamlfilename) as yamlfile:
            streamIDs = []
            picks = list()
            for p in yaml.safe_load(yamlfile):
                # Prevent duplicate stream IDs
                streamID = p["streamID"]
                duplicateStreamID = streamID in streamIDs
                if duplicateStreamID:
                    continue
                streamIDs.append(streamID)
                pick = Pick(p)
                try:
                    pick.phaseHint
                except AttributeError:
                    pick.phaseHint = "P"
                picks.append(pick)
            return picks

    def _writePicksToYaml(self, picks, yamlfilename):
        tmp = [ dict(pick) for pick in picks ]
        with open(yamlfilename, 'w') as yamlfile:
            yaml.dump(tmp, yamlfile)

    def _poll(self, reverse=True):
        """
        Check whether there is data waiting to be processed.

        * Is there a new symlink in the spool directory?
        * If yes:
            - follow the symlink
            - read the event parameters
            - process the event parameters
            - if successful and not in test mode, remove symlink
        """
        try:
            os.makedirs(self.spoolDir)
        except FileExistsError:
            pass

        spooled = self._findSpoolItems()

        # By reversing the list, we prioritize the last-added
        # items. This is good if after a long outage we want to be
        # in real-time mode quickly. But on the other hand the most
        # recent items are also usually the biggest and take
        # longest. Need to test if that has no unwanted side
        # effects. Possibly slight delays in real-time mode as
        # bigger items are prioritized, which take longer to
        # process. Or we divide big items into smaller ones. TBD
        for item in sorted(spooled, reverse=reverse):
            link, target = item

            logger.debug("+++reading %s" % target)
            adhoc_picks = self._readPicksFromYaml(target)

            # FIXME: hackish
            # The input yaml path name is composed of
            # /some/folder/name/eventID/in/oneOutOfMany.yaml
            # so the eventID is always at a fixed position in the
            # path. This is required.
            assert target.endswith(".yaml")
            eventID = target.split("/")[-3]

            try:
                logger.info("PROCESS begin")
                new_picks = self._process(adhoc_picks, eventID)
                logger.info("PROCESS end")
            except RuntimeError as e:
                logger.warning(str(e))
                continue

            if not new_picks:
                logging.warning("no results - exiting")
                os.remove(link)
                continue

            if self.test:
                logger.info("+++test mode - stopping")
                continue

            eventDir = os.path.join(self.eventRootDir, eventID)
            # directory to which we write the resulting yaml files
            out_d = os.path.join(eventDir, "out")

            outgoing_d = os.path.join(self.workingDir, "outgoing")

            os.makedirs(outgoing_d, exist_ok=True)

            d, f = os.path.split(link)

            os.makedirs(out_d, exist_ok=True)
            yamlfilename = os.path.join(out_d, f)
            self._writePicksToYaml(new_picks, yamlfilename)

            dst = os.path.join(outgoing_d, f)
            # TODO: clean up!
            src = os.path.join("..", self.eventRootDir, eventID, "out", f)

            try:
                logging.debug("creating symlink %s -> %s" % (dst, src))
                os.symlink(src, dst)
            except FileExistsError:
                logging.warning("symlink  %s -> %s" % (dst, src))

            # we are done with this item
            os.remove(link)

            # If in reverse mode, break after first processed item
            # in order to check if there are new items, which will
            # then also be processed first. If not in reverse mode
            # we don't care.
            if reverse:
                break

    def run(self):
        """Main loop"""

        while True:
            self._poll()
            time.sleep(1)

            if self.exit:
                logger.info("+++exit mode - exiting")
                break

        return True

    def _ml_predict(self, adhoc_picks, eventID):
        """ Takes a list of Pick instances, repicks them,
        fills a dictionary with those predictions,
        each a (Time, confidence) pair, and returns it.

        Returns:
            dict: a dictionary of `pickID: (time, confidence)` pairs
        """

        def fill_result(predictions, stream, collected_picks, annot_d):
            """Fills `predictions` with annotations done by the model
               using the stream. Additional data will be taken from
               `collected_picks`.
            """
            annotations, assoc_ind = None, None
            try:

                # ************ Model call ****************#
                annotations = self.model.annotate(stream)

                # Only use those predictions that were done for P wave onsets
                annotations = list(filter(
                    lambda a: a.id.split('.')[-1].endswith('_P'), annotations))

                # indexes list of successfully associated annotations
                assoc_ind = []
                for i, annotation in enumerate(annotations):
                    try:
                        # Associate the annotation to a Pick

                        pick = next(filter(
                            lambda p:
                            p.networkCode == annotation.meta.network and
                            p.stationCode == annotation.meta.station and
                            p.locationCode == annotation.meta.location,
                            collected_picks))
                    except StopIteration:
                        logger.warning(
                            "failed to associate annotation for %s.%s" % (
                                annotation.meta.network,
                                annotation.meta.station))

                        # No Pick could be found that matches the
                        # current annotation. The reason for this could be
                        # a gap in waveform data such that two traces of
                        # the same stations are passed to the model
                        # therefore the model predicts a second time, but
                        # since no Pick is waiting for it, this
                        # prediction will be discarded. This problem should
                        # be addressed in future versions by providing clean
                        # data, beforehand, because it would be too difficult
                        # to decide right here which pick is the better one
                        # resp. the one wanted.
                        continue

                    assoc_ind.append(i)
                    annot_f = os.path.join(annot_d, dotted_nslc(pick) + ".sac")
                    annotation.write(annot_f, format="SAC")

                    confidence = annotation.data.astype(np.double)
                    times = annotation.times()
                    peaks, _ = scipy.signal.find_peaks(confidence, height=0.1)
                    for peak in peaks:
                        picktime = annotation.stats.starttime + times[peak]
                        if pick.publicID not in predictions:
                            predictions[pick.publicID] = []
                        new_item = (picktime, confidence[peak])
                        predictions[pick.publicID].append(new_item)
                        logger.debug("#### " + pick.publicID + "  %.3f" % confidence[peak])

                    collected_picks.remove(pick)

            except (TypeError, ValueError, ZeroDivisionError) as e:
                logger.error("Caught "+repr(e))

            if None not in [annotations, assoc_ind]:

                # Clean annotations from those who were associated successfully
                [annotations.pop(i) for i in sorted(assoc_ind, reverse=True)]

                left_annos_n = len(annotations)
                left_adhocs_n = len(collected_picks)
                if left_annos_n > 0:
                    logger.warning(
                        f"There were {left_annos_n} annotations that "
                        "could not be associated.")
                if left_adhocs_n > 0:
                    logger.warning(
                        f"There were {left_adhocs_n} picks for "
                        "which no annotation was done.")

        # end of fill_result()


        logger.info("ML predictions starts...")

        annot_d = os.path.join(
            self.eventRootDir, eventID, self.annotDir)
        os.makedirs(annot_d, exist_ok=True)

        acc_predictions = {}
        picks_remain_size = picks_all_size = len(adhoc_picks)
        start_index, end_index = 0, min(self.batchSize, picks_all_size)

        # **** Batch loop:  *****#
        while picks_remain_size > 0:

            picks_batch = adhoc_picks[start_index:end_index]

            try:
                _eventID, stream, collected_picks = \
                    self._get_stream_from_picks(picks_batch, eventID)
            except Exception:
                stream = None

            if stream is not None:
                # In some cases no picks are returned, nonetheless this
                # could be true for the current batch of picks only, the
                # next batch could be ok, therefore we just need to pass
                # the following line
                fill_result(acc_predictions, stream, collected_picks, annot_d)

            # Updating
            picks_remain_size -= self.batchSize
            start_index += self.batchSize
            end_index = min(
                start_index + self.batchSize,
                start_index + picks_remain_size)

        logger.info("...ML prediction ended.")
        return acc_predictions


##########################################################################

# Providing strings for the available picker model classes that can be
# used as arguments for the script


def main(model_name, bs, t, e, device, wkdir, evdir, spdir, andir, dataset, conf):
    repicker = Repicker(
        model_name=model_name,
        dataset=dataset,
        test=t,
        exit=e,
        batchSize=bs,
        workingDir=wkdir,
        eventRootDir=evdir,
        spoolDir=spdir,
        annotDir=andir,
        device=device,
        minConfidence=conf
    )
    repicker.run()


if __name__ == '__main__':
    models = list(MODEL_MAP.keys())
    parser = argparse.ArgumentParser(description='SeicComp Client - ML Repicker using SeisBench')
    parser.add_argument(
        '--model', choices=models, default=models[0], dest='model',
        help=f"Choose one of the available ML models to make the predictions."
             f" Note that if the model is not cached, it might take a " \
             f"little while to download the weights file.")
    parser.add_argument(
        '--test', action='store_true',
        help='Prevents the repicker from writing out outgoing yaml with refined picks.')
    parser.add_argument(
        '--exit', action='store_true',
        help='Exit after items in spool folder have been processed')
    parser.add_argument(
        '--bs', '--batch-size', action='store_const', const=50, default=50, dest='batchSize',
        help="Choose a batch size that is suitable for the machine you are working on. Defaults to 50.")
    parser.add_argument(
        '--device', choices=['cpu', 'gpu'], default='cpu',
        help="If you have access to cuda device change this parameter to 'gpu'.")
    parser.add_argument(
        '--working-dir', type=str, default='.', dest='workingDir',
        help="Working directory where all files are placed and exchanged")
    parser.add_argument(
        '--event-dir', type=str, default='', dest='eventRootDir',
        help="Where to look for event folders with waveforms and picks and where to store annotations "
            "per each event")
    parser.add_argument(
        '--spool-dir', type=str, default='', dest='spoolDir',
        help="Where to look for new symlinks to YAML files that can be processed by the repicker.")
    parser.add_argument(
        '--annot-dir', type=str, default="annot", dest='annotDir',
        help="Where to write the annotations to, inside events/<event>/.")
    parser.add_argument(
        '--outgoing-dir', type=str, default='', dest='outgoingDir',
        help="outgoing directory where all result files are written")
    parser.add_argument(
        '--dataset', type=str, default='geofon', dest='dataset',
        help="The dataset on which the model was predicted. Defaults to geofon.")
    parser.add_argument(
        '--min-confidence', type=float, default=0.3, dest='minConfidence',
        help="Confidence threshold below which a pick is skipped. Defaults to 0.3")
    args = parser.parse_args()

    if not args.eventRootDir:
        args.eventRootDir = os.path.join(args.workingDir, "events")
    if not args.spoolDir:
        args.spoolDir = os.path.join(args.workingDir, "spool")
    if not args.outgoingDir:
        args.outgoingDir = os.path.join(args.workingDir, "outgoing")

    main(
        args.model,
        bs=args.batchSize,
        t=args.test,
        e=args.exit,
        device=args.device,
        wkdir=args.workingDir,
        evdir=args.eventRootDir,
        spdir=args.spoolDir,
        andir=args.annotDir,
        dataset=args.dataset,
        conf=args.minConfidence
    )
