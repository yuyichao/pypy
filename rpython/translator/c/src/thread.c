/* Thread implementation */
#include "src/thread.h"

#ifdef PYPY_USING_BOEHM_GC
/* The following include is required by the Boehm GC, which apparently
 * crashes when pthread_create_thread() is not redefined to call a
 * Boehm wrapper function instead.  Ugly.
 */
#include "common_header.h"
#endif


/* More ugliness follows... */
#ifdef RPY_FASTGIL
# if RPY_FASTGIL == 42    /* special value to mean "asmgcc" */
#  include "common_header.h"
#  include "structdef.h"
#  include "forwarddecl.h"
# endif
#endif


#ifdef _WIN32
#include "src/thread_nt.c"
#else
#include "src/thread_pthread.c"
#endif

