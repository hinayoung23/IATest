#import <XCTest/XCTest.h>
@interface IATestVideoServer : NSObject
- (instancetype)initWithApp:(XCUIApplication *)app environment:(NSDictionary *)environment;
- (instancetype)initWithApp:(XCUIApplication *)app environment:(NSDictionary *)environment limits:(NSDictionary *)limits;
- (void)start;
- (void)close;
@end
