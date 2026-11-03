#import <Foundation/Foundation.h>
#import <math.h>

static inline BOOL IATestVisualFresh(double captured,double latest,double now,double maxAge) {
    return isfinite(captured)&&isfinite(latest)&&isfinite(now)&&captured>0&&maxAge==2000&&
        fabs(captured-latest)<0.001&&now>=captured&&now-captured<=maxAge;
}
