#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
#include <math.h>

static inline BOOL IATestBoundsValid(id value) {
    if(![value isKindOfClass:NSArray.class]||[value count]!=4)return NO;
    for(id number in value)if(![number isKindOfClass:NSNumber.class]
        ||CFGetTypeID((__bridge CFTypeRef)number)==CFBooleanGetTypeID()
        ||!isfinite([number doubleValue])||fabs([number doubleValue])>32768)return NO;
    return [value[2] doubleValue]>[value[0] doubleValue]&&[value[3] doubleValue]>[value[1] doubleValue];
}
static inline BOOL IATestBoundsMatch(CGRect frame,NSArray *bounds) {
    return IATestBoundsValid(bounds)&&!CGRectIsEmpty(frame)&&!CGRectIsNull(frame)
        &&frame.origin.x==[bounds[0] doubleValue]&&frame.origin.y==[bounds[1] doubleValue]
        &&CGRectGetMaxX(frame)==[bounds[2] doubleValue]&&CGRectGetMaxY(frame)==[bounds[3] doubleValue];
}
