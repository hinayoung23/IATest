#if DEBUG
#import <Foundation/Foundation.h>
#import <ReplayKit/ReplayKit.h>
/** Development App only. Requires explicit screen-recording consent; media is read-only. */
@interface IATestReplayStream : NSObject
- (instancetype)initWithPort:(uint16_t)port token:(NSString *)token;
- (void)startWithCompletion:(void (^)(NSError *error))completion;
- (void)close;
@end
#endif
