#import "NetworkTransport.h"
int main(void) {
    @autoreleasepool {
        IATestSocket channel={.fd=-1,.tls=NULL};
        @try {
            channel=IATestConnectObservation(NSProcessInfo.processInfo.environment,@"control");
            char bytes[4];
            if(!IATestSocketRead(&channel,bytes,4)||memcmp(bytes,"ping",4)!=0||!IATestSocketWrite(&channel,"pong",4))return 3;
            return 0;
        } @catch(NSException *error) {return 2;}
        @finally {IATestSocketClose(&channel);}
    }
}
