package dev.iatest.mobile;

/** Explicit finite stream budget. Counts bytes before publishing a packet. */
final class MediaBudget {
    final long durationMs,byteLimit,started;
    private long total;
    MediaBudget(long seconds,long bytes,long now) {
        if(seconds<1||seconds>7200||bytes<1||bytes>2147483648L)throw new IllegalArgumentException("Invalid media budget");
        durationMs=seconds*1000;byteLimit=bytes;started=now;
    }
    void check(long now) {
        if(now<started||now-started>=durationMs||total>=byteLimit)throw new IllegalStateException("Media session quota exhausted");
    }
    void charge(long bytes,long now) {
        check(now);
        if(bytes<0||bytes>byteLimit-total)throw new IllegalStateException("Media byte quota exceeded");
        total+=bytes;
    }
}
