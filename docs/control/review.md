# Review runs

The **Review runs** stage plots the pressure and flow of finished runs. It reads the
saved files only; it needs no device and changes nothing on the rig.

- **Runs** lists the project's recorded runs, newest first. Tick a run to plot it.
- **Load file…** adds any fluidics CSV with the same columns, for example an older
  DropleGen log. Loaded files stay in the list until ADMET closes.
- **Channels** hides or shows Oil L, Cells M and Beads M. Each channel keeps the
  colour it has on the live plots; each run gets its own line style, shown before
  its name in the list.
- **Bin** averages readings into windows of that many seconds; *off* plots every
  reading.
- **Reset view** fits both plots to the data again.

Pressure and flow share the time axis. Step starts (▶) and ends (■) are marked as on
the live plots, for runs recorded in the project. Hovering a plot shows the time and
value under the cursor; a click pins a reference, after which the readout also shows
the difference from it, and a double-click clears it.
