//=============================================================================
// Copyright © 2025 NaturalPoint, Inc. All Rights Reserved.
// 
// THIS SOFTWARE IS GOVERNED BY THE OPTITRACK PLUGINS EULA AVAILABLE AT https://www.optitrack.com/about/legal/eula.html 
// AND/OR FOR DOWNLOAD WITH THE APPLICABLE SOFTWARE FILE(S) (“PLUGINS EULA”). BY DOWNLOADING, INSTALLING, ACTIVATING 
// AND/OR OTHERWISE USING THE SOFTWARE, YOU ARE AGREEING THAT YOU HAVE READ, AND THAT YOU AGREE TO COMPLY WITH AND ARE
// BOUND BY, THE PLUGINS EULA AND ALL APPLICABLE LAWS AND REGULATIONS. IF YOU DO NOT AGREE TO BE BOUND BY THE PLUGINS
// EULA, THEN YOU MAY NOT DOWNLOAD, INSTALL, ACTIVATE OR OTHERWISE USE THE SOFTWARE AND YOU MUST PROMPTLY DELETE OR
// RETURN IT. IF YOU ARE DOWNLOADING, INSTALLING, ACTIVATING AND/OR OTHERWISE USING THE SOFTWARE ON BEHALF OF AN ENTITY,
// THEN BY DOING SO YOU REPRESENT AND WARRANT THAT YOU HAVE THE APPROPRIATE AUTHORITY TO ACCEPT THE PLUGINS EULA ON
// BEHALF OF SUCH ENTITY. See license file in root directory for additional governing terms and information.
//=============================================================================


/*********************************************************************
 * \page   OpenGlDrawingFunctions.h
 * \file   OpenGlDrawingFunctions.h
 * \brief  Functions for simple drawing into the current OpenGL rendering context.
 *********************************************************************/


#ifndef _OPEN_GL_DRAWING_FUNCTIONS_H_
#define _OPEN_GL_DRAWING_FUNCTIONS_H_

#include <Windows.h>
#include <gl/GL.h>
#include <gl/GLU.h>
#include <cmath>

//////////////////////////////////////////////////////////////////////////
/// Functions for simple drawing into the current OpenGL rendering context.
//////////////////////////////////////////////////////////////////////////
class OpenGLDrawingFunctions
{
public:
  // Constants used by the drawing functions.
  static const float X1;
  static const float Z1;
  static const float vdata[12][3];
  static const int tindices[20][3];

  //////////////////////////////////////////////////////////////////////////
  /// <summary>Normalizes a 3-vector to unit length.</summary>
  //////////////////////////////////////////////////////////////////////////
  static void Normalize(GLfloat *a) 
  {
    GLfloat d = (1.0F / std::sqrt(a[0]*a[0]+a[1]*a[1]+a[2]*a[2]));
    a[0] *= d; a[1] *= d; a[2] *= d;
  }

  static void DrawTriangle(const GLfloat *a, const GLfloat *b, const GLfloat *c, int div, float r);

  static void DrawSphere(int ndiv, float radius=1.0);

  static void DrawBox(GLfloat x, GLfloat y, GLfloat z, GLfloat qx, GLfloat qy, GLfloat qz, GLfloat qw);

  static void DrawGrid();

  static void DrawCube(float scale);

};

#endif // _OPEN_GL_DRAWING_FUNCTIONS_H_