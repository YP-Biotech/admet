# Review runs

The **Review runs** stage plots the pressure and flow of finished runs. It reads the
saved files only; it needs no device and changes nothing on the rig.

- The **runs table** lists the project's recorded runs, newest first, with when they
  were recorded, how they ended and how long they ran. Tick a run to plot it; its
  line style appears in the *Line* column.
- **Load file…** adds any fluidics CSV with the same columns, for example an older
  DropleGen log. Loaded files stay in the table until ADMET closes.
- **Channels** hides or shows Oil L, Cells M and Beads M. Each channel keeps the
  colour it has on the live plots.
- **Bin** averages readings into windows of that many seconds; *off* plots every
  reading.
- **Align** lines runs up on their first protocol step (▶ 1); a loaded file, which has
  no steps, on its first row:
  - *To first run* lines up runs only: the top ticked run keeps its own clock and every
    other run moves, whole, so its first step lands on the same point;
  - *Stacked* lines up steps too: every step of every run, from its start to its end,
    is drawn from 0, so steps and replicates overlay;
  - *Recorded time* shows each run as recorded, in seconds since ADMET began reading
    the Fluigent.
- **Reset view** fits both plots to the data again.

Pressure and flow share the time axis. Step starts (▶) and ends (■) are marked as on
the live plots, for runs recorded in the project. A crosshair follows the cursor with
the time and value under it; a click pins a reference (+), after which the readout
also shows the difference from it, and a double-click clears it.

**Ctrl + wheel** over any plot zooms it, here and on the live plots; the wheel alone
scrolls the page.
