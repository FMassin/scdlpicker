#!/usr/bin/env seiscomp-python
# -*- coding: utf-8 -*-
###########################################################################
# Copyright (C) GFZ Potsdam                                               #
# All rights reserved.                                                    #
#                                                                         #
# Author: Joachim Saul (saul@gfz-potsdam.de)                              #
#                                                                         #
# GNU Affero General Public License Usage                                 #
# This file may be used under the terms of the GNU Affero                 #
# Public License version 3.0 as published by the Free Software Foundation #
# and appearing in the file LICENSE included in the packaging of this     #
# file. Please review the following information to ensure the GNU Affero  #
# Public License version 3.0 requirements will be met:                    #
# https://www.gnu.org/licenses/agpl-3.0.html.                             #
###########################################################################

"""
This is a very simple online relocator that

- listens to the messaging for origins
- reads the preferred origin of an event from the database
- tries to find all matching DL picks based on predicted
  travel times
- relocates based on *only* the DL-picks
- sends the results
"""


import sys
import pathlib
import traceback
import seiscomp.core
import seiscomp.client
import seiscomp.datamodel
import seiscomp.logging
import seiscomp.math
import seiscomp.seismology
import scdlpicker.dbutil as _dbutil
import scdlpicker.util as _util
import scdlpicker.relocation as _relocation
import scdlpicker.defaults as _defaults
import scdlpicker.depth as _depth
import scstuff.dbutil



def quality(origin):
    # similar role of origin score in scautoloc
    return _util.arrivalCount(origin)  # to be improved


def getFixedDepth(origin):
    # Quick hack for SW Poland copper mining region as a test.
    # TODO: Configurable solution needed!
    lat, lon = origin.latitude().value(), origin.longitude().value()
    if 50 <= lat <= 52 and 15 <= lon <= 20:
        return 1


class App(seiscomp.client.Application):

    def __init__(self, argc, argv):
        argv = argv.copy()
        argv[0] = "scdlpicker"

        super(App, self).__init__(argc, argv)

        self.setMessagingEnabled(True)
        self.setDatabaseEnabled(True, True)
        self.setLoadInventoryEnabled(True)
        self.setPrimaryMessagingGroup("LOCATION")
        self.addMessagingSubscription("LOCATION")
        self.addMessagingSubscription("EVENT")

        self.minDepth = _defaults.minDepth
        self.minDelay = 20*60  # 20 minutes!
        self.device = "cpu"

        self.pickAuthors = _defaults.pickAuthors

        # Keep track of changes of the preferred origin of each event
        self.preferredOrigins = dict()

        # Keep track of events that need to be processed. We process
        # one event at a time. In this dict we register the events
        # that require processing but we delay processing until
        # previous events are finished.
        self.pendingEvents = dict()

        self.origins = dict()

        # latest relocated origin per event
        self.relocated = dict()

        now = seiscomp.core.Time.GMT()
        self._previousPingDB = now

    def createCommandLineDescription(self):
        super(App, self).createCommandLineDescription()

        self.commandline().addGroup("Config")
        self.commandline().addStringOption(
            "Config", "working-dir,d", "Path of the working directory where intermediate files are placed and exchanged")
        self.commandline().addStringOption(
            "Config", "device", "'cpu' or 'gpu'. Default is 'cpu' but with access to a cuda device you can change this parameter to 'gpu'")

        self.commandline().addStringOption(
            "Config", "author", "Author of created objects")
        self.commandline().addStringOption(
            "Config", "agency", "Agency of created objects")

        self.commandline().addGroup("Target")
        self.commandline().addStringOption(
            "Target", "event,E", "process the specified event and exit")
        self.commandline().addStringOption(
            "Target", "pick-authors",
            "space-separated whitelist of pick authors")
        self.commandline().addDoubleOption(
            "Target", "max-residual",
            "limit the individual pick residual to the specified value "
            "(in seconds)")
        self.commandline().addDoubleOption(
            "Target", "min-delay",
            "Minimum delay (in seconds) after origin time before a relocation "
            "is attempted")
        self.commandline().addDoubleOption(
            "Target", "max-rms",
            "limit the pick residual RMS to the specified value (in seconds)")
        self.commandline().addOption(
            "Target", "test", "test mode - don't send the result")

    def initConfiguration(self):
        # Called before validateParameters()

        if not super(App, self).initConfiguration():
            return False

        try:
            self.workingDir = self.configGetString("scdlpicker.workingDir")
        except RuntimeError:
            self.workingDir = _defaults.workingDir

        try:
            self.pickAuthors = self.configGetDouble("scdlpicker.relocation.pickAuthors")
        except RuntimeError:
            pickAuthors = ["dlpicker"]
        self.pickAuthors = list(self.pickAuthors)

        try:
            self.minDelay = self.configGetDouble("scdlpicker.relocation.minDelay")
        except RuntimeError:
            self.minDelay = _defaults.minDelay

        try:
            self.minDepth = self.configGetDouble("scdlpicker.relocation.minDepth")
        except RuntimeError:
            self.minDepth = _defaults.minDepth

        try:
            self.maxRMS = self.configGetDouble("scdlpicker.relocation.maxRMS")
        except RuntimeError:
            self.maxRMS = _defaults.maxRMS

        try:
            self.maxResidual = self.configGetDouble("scdlpicker.relocation.maxResidual")
        except RuntimeError:
            self.maxResidual = _defaults.maxResidual

        try:
            self.maxDelta = self.configGetDouble("scdlpicker.relocation.maxDelta")
        except RuntimeError:
            self.maxDelta = _defaults.maxDelta

        try:
            self.device = self.configGetString("scdlpicker.device")
        except RuntimeError:
            self.device = _defaults.device

        return True

    def validateParameters(self):
        """
        Command-line parameters
        """
        if not super(App, self).validateParameters():
            return False

        try:
            self.minDelay = self.commandline().optionString("min-delay")
        except RuntimeError:
            pass

        try:
            self.maxResidual = self.commandline().optionDouble("max-residual")
        except RuntimeError:
            pass

        try:
            self.maxRMS = self.commandline().optionDouble("max-rms")
        except RuntimeError:
            pass

        try:
            self.device = self.commandline().optionString("device")
        except RuntimeError:
            pass

        try:
            pickAuthors = self.commandline().optionString("pick-authors")
            pickAuthors = pickAuthors.split()
        except RuntimeError:
            pickAuthors = ["dlpicker"]

        return True

    def init(self):
        if not super(App, self).init():
            return False

        self.workingDir = pathlib.Path(self.workingDir).expanduser()

        self.device = self.device.lower()
        _depth.initDepthModel(device=self.device)

        self.inventory = seiscomp.client.Inventory.Instance().inventory()

        return True

    def pingDB(self):
        """
        Keep the DB connection alive by making a dummy request every minute

        This is a temporary workaround to prevent DB connection timeouts.
        """
        now = seiscomp.core.Time.GMT()
        if float(now - self._previousPingDB) > 60:
            self.query().getObject(
                seiscomp.datamodel.Event.TypeInfo(), "dummy")
            self._previousPingDB = now

    def handleTimeout(self):
        # self.pingDB()
        self.kickOffProcessing()

    def addObject(self, parentID, obj):
        # Save new object received via messaging. The actual processing is
        # started from handleTimeout().
        self.save(obj)

    def updateObject(self, parentID, obj):
        # Save new object received via messaging. The actual processing is
        # started from handleTimeout().
        self.save(obj)

    def save(self, obj):
        # Save object for later processing in handleTimeout()
        evt = seiscomp.datamodel.Event.Cast(obj)
        if evt:
            seiscomp.logging.debug("Saving "+evt.publicID())
            if _util.valid(evt):
                self.pendingEvents[evt.publicID()] = evt
            return evt
        org = seiscomp.datamodel.Origin.Cast(obj)
        if org:
            seiscomp.logging.debug("Saving "+org.publicID())
            self.origins[org.publicID()] = org
            return org

    def kickOffProcessing(self):
        # Check for each pending event if it is due to be processed
        for eventID in sorted(self.pendingEvents.keys()):
            # seiscomp.logging.debug("kickOffProcessing begin " + eventID)
            if self.readyToProcess(eventID):
                self.pendingEvents.pop(eventID)
                self.processEvent(eventID)
        # seiscomp.logging.debug("kickOffProcessing   end " + eventID)

    def readyToProcess(self, eventID):
        """
        Before relocation we wait some time (minDelay, in seconds) to allow
        collection of all required picks. This delay differs depending on the
        network size; the default is 18 min. for global monitoring, i.e. it
        is waited until practically all P picks are usually available.
        """
        if eventID not in self.pendingEvents:
            seiscomp.logging.error("Missing event "+eventID)
            return False
        evt = self.pendingEvents[eventID]
        preferredOriginID = evt.preferredOriginID()
        if preferredOriginID not in self.origins:
            seiscomp.logging.debug("Loading origin "+preferredOriginID)
            org = _dbutil.loadOriginWithoutArrivals(
                self.query(), preferredOriginID)
            if not org:
                return False
            self.origins[preferredOriginID] = org

        org = self.origins[preferredOriginID]
        now = seiscomp.core.Time.GMT()
        dt = float(now - org.time().value())
        if dt < self.minDelay:
            return False

        try:
            author = org.creationInfo().author()
        except Exception:
            seiscomp.logging.warning(
                "Author missing in origin %s" % preferredOriginID)
            author = "MISSING"
        ownOrigin = (author == self.author)

        if ownOrigin:
            seiscomp.logging.debug(
                "I made origin "+preferredOriginID+" (nothing to do)")
            del self.pendingEvents[eventID]
            return False

        if not _util.qualified(org):
            seiscomp.logging.debug(
                "Unqualified origin "+preferredOriginID+" rejected")
            del self.pendingEvents[eventID]
            return False

        return True

    def getPicksReferencedByOrigin(self, origin, minWeight=0.5):
        picks = {}
        for i in range(origin.arrivalCount()):
            arr = origin.arrival(i)
            try:
                pickID = arr.pickID()
                if not pickID:
                    continue
                if arr.weight() < minWeight:
                    continue
            except Exception:
                continue
            pick = seiscomp.datamodel.Pick.Find(pickID)
            if not pick:
                continue
            picks[pickID] = pick
        return picks

    def comparePicks(self, origin1, origin2):
        picks1 = self.getPicksReferencedByOrigin(origin1)
        picks2 = self.getPicksReferencedByOrigin(origin2)
        common = {}
        only1 = {}
        only2 = {}

        for pickID in picks1:
            if pickID in picks2:
                common[pickID] = picks1[pickID]
            else:
                only1[pickID] = picks1[pickID]

        for pickID in picks2:
            if pickID not in picks1:
                only2[pickID] = picks2[pickID]

        return common, only1, only2

    def improvement(self, origin1, origin2):
        """
        Test if origin2 is an improvement over origin1.

        This currently only counts picks.
        It doesn't take pick status/authorship into account.
        """
        common, only1, only2 = self.comparePicks(origin1, origin2)
        count1 = len(only1) + len(common)
        count2 = len(only2) + len(common)

        seiscomp.logging.debug("count %4d ->%4d" % (count1, count2))

        try:
            rms1 = max(origin1.quality().standardError(), 1.)
        except ValueError:
            rms1 = 10.  # FIXME hotfix

        try:
            rms2 = max(origin2.quality().standardError(), 1.)
        except ValueError:
            seiscomp.logging.debug("origin2 without standardError")
            rms2 = 1.

        seiscomp.logging.debug("count %4d ->%4d" % (count1, count2))
        seiscomp.logging.debug("rms   %4.1f ->%4.1f" % (rms1, rms2))

        if count1 == 0:
            return True

        q = (count2/count1)**2 * (rms1/rms2)
        seiscomp.logging.debug("improvement  %.3f" % q)

        return q > 1

    def processEvent(self, eventID):
        event = _dbutil.loadEvent(self.query(), eventID)
        if not event:
            seiscomp.logging.warning("Failed to load event " + eventID)
            return

        seiscomp.logging.debug("Loaded event "+eventID)
        origin = _dbutil.loadOriginWithoutArrivals(
            self.query(), event.preferredOriginID())
        seiscomp.logging.debug("Loaded origin " + origin.publicID())

        # Adopt fixed depth according to incoming origin

        # Compute fixed depth according to region.
        # E.g. regions with mostly induced seismicity.
        fixedDepth = getFixedDepth(origin)
        if fixedDepth is not None:
            defaultDepth = fixedDepth
        else:
            defaultDepth = 10.

        if _util.hasFixedDepth(origin):
            # fixed = True
            if _util.agencyID(origin) == self.agencyID and _util.statusFlag(origin) == "M":
                # At GFZ we trust the depth of manual GFZ origins. But ymmv!
                fixedDepth = origin.depth().value()
            elif origin.depth().value() == defaultDepth:
                fixedDepth = defaultDepth

        if fixedDepth is None:
            seiscomp.logging.debug("not fixing depth")
            # fixed = False
        else:
            seiscomp.logging.debug("setting fixed depth to %f km" % fixedDepth)

        # Load all picks for a matching time span, independent of association.
        maxDelta = _defaults.maxDelta
        originWithArrivals, picks = \
            _dbutil.loadPicksForOrigin(
                origin, self.inventory,
                self.pickAuthors, maxDelta, self.query())
        seiscomp.logging.debug(
            "arrivalCount=%d" % originWithArrivals.arrivalCount())

        relocated = None
        depthFromDepthPhases = None

        for attempt in ["direct", "depth phase based"]:
#       for attempt in ["direct"]:

            if attempt == "depth phase based":
                if relocated is None:
                    # No successful relocation in previous run
                    seiscomp.logging.debug("no depth phase based attempt")
                    break
                if depthFromDepthPhases is None:
                    seiscomp.logging.debug("no depth phase based attempt")
                    # Depth phase depth could not be determined in previous run
                    break
                if relocated.arrivalCount() < 50:
                    seiscomp.logging.debug("no depth phase based attempt (too few picks)")
                    # Don't enter 2nd round for small events. criteria t.b.d.
                    break
                if relocated.depth().value() > 120:
                    seiscomp.logging.debug("no depth phase based attempt (depth > 120)")
                    # temporarily
                    break

                # adopt the previous relocation result
                originWithArrivals = relocated
                fixedDepth = depthFromDepthPhases

            relocated = _relocation.relocate(
                originWithArrivals, eventID, fixedDepth,
                self.minDepth, self.maxResidual)
            if not relocated:
                seiscomp.logging.warning("%s: relocation failed" % eventID)
                return
            if relocated.arrivalCount() < 5:
                seiscomp.logging.info("%s: too few arrivals" % eventID)
                return

            now = seiscomp.core.Time.GMT()
            ci = _util.creationInfo(self.author, self.agencyID, now)
            relocated.setCreationInfo(ci)
            relocated.setEvaluationMode(seiscomp.datamodel.AUTOMATIC)
            self.origins[relocated.publicID()] = relocated

            _util.summarize(relocated)

            if attempt == "direct":
                if eventID in self.relocated:
                    # if quality(relocated) <= quality(self.relocated[eventID]):
                    if not self.improvement(self.relocated[eventID], relocated):
                        seiscomp.logging.info(
                            "%s: no improvement - origin not sent" % eventID)
                        return

            ep = seiscomp.datamodel.EventParameters()
            seiscomp.datamodel.Notifier.Enable()
            ep.add(relocated)
            event.add(seiscomp.datamodel.OriginReference(relocated.publicID()))
            msg = seiscomp.datamodel.Notifier.GetMessage()
            seiscomp.datamodel.Notifier.Disable()

            if self.commandline().hasOption("test"):
                seiscomp.logging.info(
                    "test mode - not sending " + relocated.publicID())
            else:
                if self.connection().send(msg):
                    seiscomp.logging.info("sent " + relocated.publicID())
                else:
                    seiscomp.logging.info("failed to send " + relocated.publicID())

            self.relocated[eventID] = relocated

            if attempt == "depth phase based":
                # no 2nd attempt using depth phases
                break

            # Experimental depth computation. Logging only.
            seiscomp.logging.debug("Computing depth for event " + eventID)
            q = self.query()
            ep = scstuff.dbutil.loadCompleteEvent(q, eventID, withPicks=True, preferred=True)
            for iorg in range(ep.originCount()):
                org = ep.origin(iorg)
                q.loadArrivals(org)  # TEMP HACK!!!!

            # FIXME:
            workingDir = pathlib.Path("~/scdlpicker").expanduser()
            try:
                depthFromDepthPhases = _depth.computeDepth(ep, eventID, workingDir, seiscomp_workflow=True)
                # depthFromDepthPhases = _depth.computeDepth(ep, eventID, workingDir, seiscomp_workflow=True, picks=picks)
            except Exception as e:
                seiscomp.logging.warning("Caught exception %s" % e)
                traceback.print_exc()
                depthFromDepthPhases = None
            t = seiscomp.core.Time.GMT().toString("%F %T")
            with open(workingDir / "depth.log", "a") as f:
                if depthFromDepthPhases is not None:
                    seiscomp.logging.info("DEPTH=%.1f" % depthFromDepthPhases)
                    f.write("%s %s   %5.1f km\n" % (t, eventID, depthFromDepthPhases))
                else:
                    seiscomp.logging.error("DEPTH COMPUTATION FAILED for "+eventID)
                    f.write("%s %s   depth computation failed\n" % (t, eventID))


    def run(self):
        seiscomp.datamodel.PublicObject.SetRegistrationEnabled(True)

        try:
            self.author = self.commandline().optionString("author")
        except RuntimeError:
            self.author = "dl-reloc"

        try:
            self.agencyID = self.commandline().optionString("agency")
        except RuntimeError:
            self.agencyID = "GFZ"

        try:
            eventIDs = self.commandline().optionString("event").split()
        except RuntimeError:
            eventIDs = None

        if eventIDs:
            # immediately process all events and exit
            for eventID in eventIDs:
                self.processEvent(eventID)
            return True

        # enter online mode
        self.enableTimer(1)
        return super(App, self).run()


if __name__ == "__main__":
    app = App(len(sys.argv), sys.argv)
    status = app()
    sys.exit(status)
