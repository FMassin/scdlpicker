#!/bin/bash

# Invocation of the scdlpicker.relocate-event.py


server="localhost"
user="NLoDL_aloc"
agency="SED"
group="LOCATION_NLoDL"

# IF EVENT IS NOT NEW SKIP IT
#if [[ $2 -ne 1 ]] ; then
#    exit 1
#fi

# IF EVENT IS ALREADY BEING PROCESSED SKIP IT
PID=$$
ps -ef|grep -v $PID|grep $3|grep $0 && exit 1

# IF EVENT IS PROCESS ALREADY SKIP IT
grep $3 ~/.seiscomp/log/${user}.* && exit 1

# WAIT 3 MIN FOR REPICKING
echo sleep 3 min for repicking...
sleep  $(( 3*60 ))

/usr/local/bin/seiscomp exec /usr/local/bin/scdlpicker.relocate-event.py --debug \
  -H $server -u $user \
  --agency=$agency \
  --author=$user@$HOSTNAME  \
  --primary-group=$group \
  --event $3  2>&1 |
  tee ~/.seiscomp/log/${user}.`date  +'%Y%m%d-%H%M%S'`.log

